"""log-analyzer の HTTP API（管理 UI 用 backend）。

起動:
    cd apps/agents
    uvicorn log_analyzer.api:app --reload --port 8000

エンドポイント:
    GET    /api/configs                       構成一覧（builtin + user）
    GET    /api/logs                          samples/logs/ にあるログ一覧
    GET    /api/prompt-slots/{base_config}    base_config のプロンプト slot 定義
    GET    /api/configs/saved                 ユーザー定義構成一覧
    POST   /api/configs/saved                 ユーザー定義構成を新規保存
    PUT    /api/configs/saved/{id}            上書き更新
    DELETE /api/configs/saved/{id}            削除
    POST   /api/runs                          指定構成を指定ログに当て、AnalysisResult を返す
    POST   /api/runs/stream                   構成4 (rally) を SSE でストリーミング実行
    POST   /api/runs/topology-stream          トポロジー + ノード別ログを構成4 で SSE 実行
    POST   /api/runs/config-log-stream        config-log 解析 (1 段階 / 2 段階) を SSE 実行
    GET    /api/audit-prompt                  GPT 監査の既定システムプロンプト (編集欄の初期値)
    POST   /api/analysis-history              解析履歴を保存 (完全再現用、config-log 完了時)
    GET    /api/analysis-history              解析履歴一覧 (サマリ)
    GET    /api/analysis-history/{id}         解析履歴 個別取得 (request/result 込み)
    DELETE /api/analysis-history/{id}         解析履歴 削除
    POST   /api/runs/{run_id}/decision        確認モーダルからの継続 / 停止 / 進む / 中止指示を送る
    GET    /api/questionnaires                問診票テンプレート一覧 (Phase B)
    POST   /api/questionnaires                問診票テンプレート新規作成
    GET    /api/questionnaires/{id}           問診票テンプレート取得
    PUT    /api/questionnaires/{id}           問診票テンプレート更新
    DELETE /api/questionnaires/{id}           問診票テンプレート削除 (default は不可)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from log_analyzer import pipeline_runner, prompt_slots, storage
from log_analyzer.cli import CONFIG_RUNNERS
from log_analyzer.rally_agent import StreamEvent, run_rally_stream
from log_analyzer.rally_two_stage import run_two_stage_stream
from log_analyzer.schema import AnalysisResult, QuestionnaireItem, QuestionnaireTemplate, StageOutput

load_dotenv()
storage.init_db()

app = FastAPI(title="log-analyzer API", version="0.2.0")

# 開発用フロントエンドの origin を許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# このファイルは apps/agents/src/log_analyzer/api.py、リポジトリルートは 4 階層上
_REPO_ROOT = Path(__file__).resolve().parents[4]
_LOGS_DIR = _REPO_ROOT / "samples" / "logs"

# UI 表示用の説明的ラベル。コード ID（config1..config5）はトレース名・DB・JSON で固定。
# モデル名は slot 別に上書き可能なのでラベルには含めない（パイプライン構造のみ表現）。
BUILTIN_CONFIG_LABELS: dict[str, str] = {
    "config1": "config1 — ベースライン（単一 LLM）",
    "config2": "config2 — フィルタ + 圧縮（前処理 → 分析の 2 段）",
    "config3": "config3 — マルチモデル並列（3 モデル並列 → 統合）",
    "config4": "config4 — オーケストレータ駆動（LangGraph orchestrator が監視を再評価しながらラリー）",
    "config5": "config5 — ユーザー定義パイプライン（DAG 自由設計）",
    "config-log": "config-log — config-log 解析（1 段階 / 2 段階。表示専用 / 実行は専用タブから）",
}

# 選択 UI から除外する builtin。config5 は pipeline_def 必須なので、
# 直接 builtin として選んでも実行不可。ユーザーは構成設計タブで pipeline を作成し、
# user:N として保存してから利用する。
HIDDEN_BUILTIN_CONFIGS: set[str] = {"config5"}


# config1〜4 の固定グラフ構造（React Flow 描画用）。
# 各ノードは type / slot_id / fixed_model を持つ:
#   - type=input: 入力ノード（編集不可）
#   - type=slot: 通常の slot ノード（slot_id でプロンプト/モデル編集可）
#   - type=slot_instance: 複数モデル並列で同じ slot を共有する場合（モデルは固定）
BUILTIN_STRUCTURES: dict[str, dict] = {
    "config1": {
        "nodes": [
            {"id": "input", "type": "input", "label": "入力ログ"},
            {"id": "analyze", "type": "slot", "slot_id": "analyze", "label": "分析（最終出力）"},
        ],
        "edges": [
            {"source": "input", "target": "analyze"},
        ],
    },
    "config2": {
        "nodes": [
            {"id": "input", "type": "input", "label": "入力ログ"},
            {"id": "filter_stage", "type": "static", "label": "ルールフィルタ\n(deterministic)"},
            {"id": "triage", "type": "slot", "slot_id": "triage", "label": "Triage 圧縮"},
            {"id": "analyze", "type": "slot", "slot_id": "analyze", "label": "分析（最終出力）"},
        ],
        "edges": [
            {"source": "input", "target": "filter_stage"},
            {"source": "filter_stage", "target": "triage"},
            {"source": "triage", "target": "analyze"},
        ],
    },
    "config3": {
        "nodes": [
            {"id": "input", "type": "input", "label": "入力ログ"},
            {
                "id": "sonnet",
                "type": "slot_instance",
                "slot_id": "analyze",
                "fixed_model": "claude-sonnet-4-5",
                "label": "Sonnet\n(slot: analyze)",
            },
            {
                "id": "haiku",
                "type": "slot_instance",
                "slot_id": "analyze",
                "fixed_model": "claude-haiku-4-5",
                "label": "Haiku\n(slot: analyze)",
            },
            {
                "id": "openai",
                "type": "slot_instance",
                "slot_id": "analyze",
                "fixed_model": "gpt-4o-mini",
                "label": "GPT-4o-mini\n(slot: analyze)",
            },
            {"id": "integrate", "type": "slot", "slot_id": "integrate", "label": "統合（最終出力）"},
        ],
        "edges": [
            {"source": "input", "target": "sonnet"},
            {"source": "input", "target": "haiku"},
            {"source": "input", "target": "openai"},
            {"source": "sonnet", "target": "integrate"},
            {"source": "haiku", "target": "integrate"},
            {"source": "openai", "target": "integrate"},
        ],
    },
    # 編集画面のワークフロー図 (委譲チェーン型 5 監視構成):
    # - orchestrator は初回 1 つの監視を選ぶ（5 本の実線）
    # - 各監視は次の監視 or integrator を 1 つ指名する（破線 kind=delegation で表現）
    # - 監視同士の委譲は完全グラフ（5 監視 × 4 = 20 エッジ）になり煩雑なので、
    #   代表的な遷移として隣接リング (fw↔routing↔app↔dns↔sec) を破線で示す
    # - 実際に通った経路は実行結果カードの execution_graph で確認できる
    "config4": {
        "nodes": [
            {"id": "input", "type": "input", "label": "入力ログ"},
            {"id": "orchestrator", "type": "slot", "slot_id": "orchestrator", "label": "オーケストレータ\n(初回 1 回のみ・最初の監視を 1 つ選ぶ)"},
            {"id": "fw_monitor", "type": "slot", "slot_id": "fw_monitor", "label": "FW 監視\n(次ノードを指名)"},
            {"id": "routing_monitor", "type": "slot", "slot_id": "routing_monitor", "label": "Routing 監視\n(次ノードを指名)"},
            {"id": "app_monitor", "type": "slot", "slot_id": "app_monitor", "label": "App 監視\n(次ノードを指名)"},
            {"id": "dns_monitor", "type": "slot", "slot_id": "dns_monitor", "label": "DNS 監視\n(次ノードを指名)"},
            {"id": "sec_monitor", "type": "slot", "slot_id": "sec_monitor", "label": "Security 監視\n(次ノードを指名)"},
            {"id": "integrator", "type": "slot", "slot_id": "integrator", "label": "統合（最終出力）"},
        ],
        "edges": [
            # 正方向のフロー（orchestrator → 5 監視のうち 1 つを選ぶ）
            {"source": "input", "target": "orchestrator"},
            {"source": "orchestrator", "target": "fw_monitor", "label": "初手選択"},
            {"source": "orchestrator", "target": "routing_monitor", "label": "初手選択"},
            {"source": "orchestrator", "target": "app_monitor", "label": "初手選択"},
            {"source": "orchestrator", "target": "dns_monitor", "label": "初手選択"},
            {"source": "orchestrator", "target": "sec_monitor", "label": "初手選択"},
            # 各監視 → integrator (監視自身が finalize を選んだ場合)
            {"source": "fw_monitor", "target": "integrator", "label": "finalize"},
            {"source": "routing_monitor", "target": "integrator", "label": "finalize"},
            {"source": "app_monitor", "target": "integrator", "label": "finalize"},
            {"source": "dns_monitor", "target": "integrator", "label": "finalize"},
            {"source": "sec_monitor", "target": "integrator", "label": "finalize"},
            # 監視 → 監視 の委譲（代表的なリング、破線で描画）
            {"source": "fw_monitor", "target": "routing_monitor", "kind": "feedback", "label": "委譲"},
            {"source": "routing_monitor", "target": "app_monitor", "kind": "feedback", "label": "委譲"},
            {"source": "app_monitor", "target": "dns_monitor", "kind": "feedback", "label": "委譲"},
            {"source": "dns_monitor", "target": "sec_monitor", "kind": "feedback", "label": "委譲"},
            {"source": "sec_monitor", "target": "fw_monitor", "kind": "feedback", "label": "委譲"},
        ],
    },
    # config-log 解析の **メタ構造**（単一実行タブで表示専用に出す）。
    # 各 Stage の内部 rally は config4 と同じ構造なので、ここでは Stage を 1 ノード
    # にまとめ「2 段階 + 人間承認 + (任意) GPT 監査」のフローを高レベルに示す
    # (2 段階モード config→log を代表例として描画。1 段階モードや log→config 順も
    #  同じ部品の組み替えで表現される)。
    # 実行は専用「config-log 解析」タブから (executable_from_single=false)。
    "config-log": {
        "nodes": [
            # 入力レイヤ (横並びの 4 つ)
            {"id": "input_topology", "type": "input", "label": "構成図\n(画像 + ノード矩形)"},
            {"id": "input_configs", "type": "input", "label": "ノード別 Configs"},
            {"id": "input_logs", "type": "input", "label": "ノード別 Logs"},
            {"id": "input_questionnaire", "type": "input", "label": "問診票回答\n(Phase B)"},
            # メタフロー本体
            {"id": "stage_one", "type": "static",
             "label": "Stage 1: 一方で解析\n(例: rally on configs only)\n→ suspected_nodes 仮説"},
            {"id": "human_approval", "type": "static",
             "label": "人間承認モーダル\n(advance / abort)"},
            {"id": "stage_two", "type": "static",
             "label": "Stage 2: もう一方で検証\n(例: rally on logs + Stage 1 仮説)"},
            {"id": "audit", "type": "static",
             "label": "GPT 監査\n(Phase C, 任意)"},
            {"id": "final", "type": "static",
             "label": "最終 AnalysisResult\nstage_outputs[] / suspected_node_findings\n/ audit_report / round_metrics"},
        ],
        "edges": [
            # 入力 → Stage 1
            {"source": "input_topology", "target": "stage_one"},
            {"source": "input_configs", "target": "stage_one"},
            {"source": "input_questionnaire", "target": "stage_one"},
            # Stage 1 → 人間承認
            {"source": "stage_one", "target": "human_approval", "label": "stage_one_complete"},
            # 人間承認 → Stage 2 (advance)
            {"source": "human_approval", "target": "stage_two", "label": "advance"},
            # 人間承認 → 最終 (abort、破線)
            {"source": "human_approval", "target": "final", "label": "abort", "kind": "feedback"},
            # Stage 2 の追加入力
            {"source": "input_logs", "target": "stage_two"},
            {"source": "input_topology", "target": "stage_two"},
            # Stage 2 → 監査 (任意)
            {"source": "stage_two", "target": "audit", "label": "audit_after_integrator=true"},
            # Stage 2 → 最終 (監査 OFF、破線)
            {"source": "stage_two", "target": "final", "label": "audit OFF", "kind": "feedback"},
            # 監査 → 最終
            {"source": "audit", "target": "final"},
        ],
    },
}

# 比較ビュー（Phase 2 W6）で 4 構成同時実行することを想定し max_workers=4。
# 各 runner 内部でもモデル API を並列呼び出しするので、ピーク時の同時リクエスト数は 10+ になる
# （Anthropic / OpenAI のレート制限内に収まる前提）。
_executor = ThreadPoolExecutor(max_workers=4)


class ConfigEntry(BaseModel):
    id: str  # "config1" / "user:<id>" / "config-log"
    label: str
    # "builtin": 単一実行/比較タブから実行可
    # "user":    saved_configs から作成されたユーザー定義
    # "builtin_view_only": 構成図表示のみ。実行は専用タブから (config-log 等)
    type: str
    base_config: str  # "config1" .. "config4" / "config-log"


class ConfigsResponse(BaseModel):
    configs: list[ConfigEntry]


class LogEntry(BaseModel):
    name: str
    bytes: int
    lines: int
    modified_at: str  # ISO8601 (UTC)


class LogsResponse(BaseModel):
    logs: list[LogEntry]


class LogContentResponse(BaseModel):
    name: str
    bytes: int
    total_lines: int
    preview_lines: int  # 実際に返した行数
    truncated: bool
    content: str  # 先頭 preview_lines 行をくっつけたテキスト


class LogUploadResponse(BaseModel):
    name: str
    bytes: int
    lines: int


# アップロード制限
_MAX_LOG_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
_PREVIEW_MAX_LINES = 200  # プレビューで返す最大行数
# パストラバーサル防止のためファイル名は英数字 / _ - . のみ許可
_VALID_LOG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.log$")


def _safe_log_path(name: str) -> Path:
    """``name`` を検証し、samples/logs/ 配下の安全な絶対パスを返す。

    パスセパレータや `..` を含む名前、`.log` 以外の拡張子は弾く。
    """
    if not _VALID_LOG_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "ファイル名は英数字 / _ / - / . のみ、拡張子は .log を必須とします: "
                f"{name!r}"
            ),
        )
    target = (_LOGS_DIR / name).resolve()
    # _LOGS_DIR の外を指していないことを念のため確認（_VALID_LOG_NAME_RE で
    # パスセパレータは弾いているが多重防御）
    if _LOGS_DIR.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail=f"invalid log path: {name}")
    return target


class RunRequest(BaseModel):
    log_name: str
    config: str  # "config1" .. "config5" / "user:<id>"
    # ad-hoc overrides（builtin config 選択時に編集中の slot を即時試す経路）。
    # config が "user:<id>" の場合は無視され、保存済みの値が使われる。
    overrides: dict[str, str] | None = None
    model_overrides: dict[str, str] | None = None
    # config5（user_pipeline）でのみ使用。ad-hoc プレビュー実行時にここで pipeline_def を渡す。
    pipeline: dict | None = None
    # config4（rally）専用ランタイムパラメータ。
    rally_max_rounds: int | None = None


class DecisionRequest(BaseModel):
    """確認モーダルからの応答。

    rally_max_rounds 上限到達時:
        - "continue" (+ extend_by): rally_max_rounds を延長して再開
        - "stop": 即時 integrator 直行

    config-log 解析の 2 段階モードで Stage 1 → Stage 2 遷移時:
        - "advance": Stage 2 (検証) に進む
        - "abort":   Stage 1 で打ち切り、Stage 1 結果のみを最終とする
    """

    action: str  # "continue" | "stop" | "advance" | "abort"
    extend_by: int | None = None  # action="continue" のみ。既定 3


class TopologyNode(BaseModel):
    """トポロジー解析タブで定義された 1 ノード。"""

    id: str
    type: str = ""  # "L2" / "L3" / "FW" / "Server" / 任意
    label: str = ""
    ip: str = ""


class TopologyLink(BaseModel):
    """ノード間の論理リンク（解析の参考情報、UI 描画には使わない）。"""

    source: str
    target: str


class NodeAttachmentDTO(BaseModel):
    """各ノードに添付する 1 ファイル (ログ or 設定ファイル)。"""

    name: str = ""  # ファイル名 / 識別子 (任意)
    content: str = ""


class TopologyRunRequest(BaseModel):
    """``POST /api/runs/topology-stream`` のリクエスト。

    config4 (rally) または config4 派生の user 構成のみ受け付ける。
    ``node_logs`` / ``node_configs`` は `{nodeId: [{name, content}, ...]}` の辞書で、
    1 ノードあたり複数のログ・設定ファイルを添付できる。content が空のものはスキップ。
    """

    config: str  # "config4" / "user:<id>" のみ
    topology: dict  # {"nodes": [...], "links": [...]}
    # 1 ノードに複数ログを添付できる。例: {"fw-01": [{"name": "syslog", "content": "..."}, {"name": "deny", "content": "..."}]}
    node_logs: dict[str, list[NodeAttachmentDTO]] = {}
    # 1 ノードに複数の設定ファイルを添付できる。同上の形式
    node_configs: dict[str, list[NodeAttachmentDTO]] = {}
    # 問診票回答 {key: value} (Phase B)。空でも OK
    questionnaire_answers: dict[str, str] = {}
    # 監査エージェント (Phase C) を integrator 後に走らせるか
    audit_after_integrator: bool = False
    rally_max_rounds: int | None = None
    overrides: dict[str, str] | None = None
    model_overrides: dict[str, str] | None = None


def _normalize_attachments(
    raw: dict | None,
) -> dict[str, list[dict]]:
    """``{nodeId: [{name, content}, ...]}`` を正規化。

    Pydantic 経由なら NodeAttachmentDTO のリストが渡ってくるが、テストや手動構築の
    便宜のため dict / 旧形式 (str) も受け入れる。
    """
    if not raw:
        return {}
    out: dict[str, list[dict]] = {}
    for nid, items in raw.items():
        bucket: list[dict] = []
        # 旧形式互換: 単一文字列 → 1 件のアタッチメント
        if isinstance(items, str):
            if items.strip():
                bucket.append({"name": "", "content": items})
        elif isinstance(items, list):
            for it in items:
                if hasattr(it, "model_dump"):  # Pydantic NodeAttachmentDTO
                    d = it.model_dump()
                elif isinstance(it, dict):
                    d = it
                else:
                    continue
                name = str(d.get("name") or "").strip()
                content = str(d.get("content") or "")
                if not content.strip():
                    continue
                bucket.append({"name": name, "content": content})
        if bucket:
            out[str(nid)] = bucket
    return out


def _build_questionnaire_block(answers: dict | None) -> str:
    """問診票回答ブロック (Phase B)。

    answers は {key: value} の辞書。空 / None なら "" を返す。
    UI 側で人間が記入した「症状・影響範囲・直前の変更」等を LLM の最初の
    user メッセージ冒頭に届ける。
    """
    if not answers:
        return ""
    lines: list[str] = ["## 問診票回答 (人間オペレータからの一次申告)"]
    for k, v in answers.items():
        s = str(v).strip()
        if not s:
            continue
        lines.append(f"- **{k}**: {s}")
    if len(lines) == 1:
        return ""
    lines.append("")
    return "\n".join(lines) + "\n"


def _build_topology_log_text(
    topology: dict,
    node_logs: dict | None,
    node_configs: dict | None = None,
    questionnaire_answers: dict | None = None,
) -> tuple[str, list[dict]]:
    """topology + ノード別添付 (logs / configs) から rally に渡す単一 log_text を構築。

    フォーマット:

        ## トポロジー要約
        - id=fw-01, type=FW, label=コア FW, ip=10.0.0.1
        - id=sw-l2-01, type=L2, label=配下スイッチ, ip=10.0.0.2
        リンク: fw-01 → sw-l2-01

        === NODE: fw-01 (type=FW, label=コア FW, ip=10.0.0.1) ===

        [ログ] fw-syslog.log:
        <content>

        [ログ] fw-deny.log:
        <content>

        [設定] fw-policy.conf:
        <content>

        === NODE: sw-l2-01 (...) ===
        ...

    返り値: (合成ログ, 正規化ノードのリスト)
    """
    nodes_raw = topology.get("nodes") or []
    nodes: list[dict] = []
    for n in nodes_raw:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        if not nid:
            continue
        nodes.append(
            {
                "id": str(nid),
                "type": str(n.get("type") or ""),
                "label": str(n.get("label") or ""),
                "ip": str(n.get("ip") or ""),
            }
        )
    links_raw = topology.get("links") or []
    links: list[tuple[str, str]] = []
    for lk in links_raw:
        if not isinstance(lk, dict):
            continue
        s = lk.get("source")
        t = lk.get("target")
        if s and t:
            links.append((str(s), str(t)))

    logs_map = _normalize_attachments(node_logs)
    configs_map = _normalize_attachments(node_configs)

    parts: list[str] = []
    parts.append("## トポロジー要約")
    if nodes:
        for n in nodes:
            attrs = [f"id={n['id']}"]
            if n["type"]:
                attrs.append(f"type={n['type']}")
            if n["label"]:
                attrs.append(f"label={n['label']}")
            if n["ip"]:
                attrs.append(f"ip={n['ip']}")
            parts.append("- " + ", ".join(attrs))
    else:
        parts.append("(ノード定義なし)")
    if links:
        parts.append("リンク:")
        for s, t in links:
            parts.append(f"  - {s} → {t}")
    parts.append("")

    # ノード別に複数の log / config ファイルを並べる
    valid_ids = {n["id"] for n in nodes}
    for n in nodes:
        attached_logs = logs_map.get(n["id"], [])
        attached_configs = configs_map.get(n["id"], [])
        if not attached_logs and not attached_configs:
            continue
        header_attrs = [f"type={n['type'] or '?'}", f"label={n['label'] or '?'}"]
        if n["ip"]:
            header_attrs.append(f"ip={n['ip']}")
        parts.append(f"=== NODE: {n['id']} ({', '.join(header_attrs)}) ===")
        parts.append("")
        for i, a in enumerate(attached_logs, 1):
            name = a["name"] or f"log_{i}"
            parts.append(f"[ログ] {name}:")
            parts.append(a["content"].rstrip())
            parts.append("")
        for i, a in enumerate(attached_configs, 1):
            name = a["name"] or f"config_{i}"
            parts.append(f"[設定] {name}:")
            parts.append(a["content"].rstrip())
            parts.append("")

    # 未定義 ID への添付指定は警告として残す
    orphan_ids = sorted(
        ({nid for nid in logs_map if nid not in valid_ids}
         | {nid for nid in configs_map if nid not in valid_ids})
    )
    if orphan_ids:
        parts.append("(注: 未定義の node_id に添付が指定されました: " + ", ".join(orphan_ids) + ")")

    body = "\n".join(parts) + "\n"
    # 問診票回答は最先頭に置く (LLM が最初に「人間が言ったこと」を読む構造)
    questionnaire_block = _build_questionnaire_block(questionnaire_answers)
    return (questionnaire_block + body, nodes)


class AppendLogRequest(BaseModel):
    """実行中に追加投入するログ。

    ``source`` は UI 上で由来を識別するためのラベル（ファイル名 / "inline" 等）、
    ``content`` は追加するログ本文。
    """

    content: str
    source: str = "inline"


class SlotInfo(BaseModel):
    slot_id: str
    label: str
    default_prompt: str
    default_model: str
    allowed_models: list[str]  # 空配列ならモデル上書き不可


class PromptSlotsResponse(BaseModel):
    base_config: str
    slots: list[SlotInfo]


class SavedConfigDTO(BaseModel):
    id: int
    name: str
    base_config: str
    overrides: dict[str, str]
    model_overrides: dict[str, str] = {}
    pipeline: dict | None = None
    created_at: str
    updated_at: str


class SavedConfigsResponse(BaseModel):
    configs: list[SavedConfigDTO]


class CreateSavedConfigRequest(BaseModel):
    name: str
    base_config: str
    overrides: dict[str, str] = {}
    model_overrides: dict[str, str] = {}
    pipeline: dict | None = None


class UpdateSavedConfigRequest(BaseModel):
    overrides: dict[str, str] = {}
    model_overrides: dict[str, str] = {}
    pipeline: dict | None = None


class NodeTypeDef(BaseModel):
    type: str
    label: str
    description: str
    fixed: bool
    editable_fields: list[str]
    default_prompt: str | None = None
    default_model: str | None = None
    default_input_template: str | None = None


class NodeTypesResponse(BaseModel):
    node_types: list[NodeTypeDef]
    allowed_models: list[str]


class PipelineDefaultResponse(BaseModel):
    pipeline: dict


class RuntimeConfigResponse(BaseModel):
    """UI が初期化時に取得する実行時設定（Langfuse 直リンク用 host 等）。"""

    langfuse_host: str | None = None


class RunHistoryEntry(BaseModel):
    id: int
    started_at: str  # ISO8601 (UTC)
    log_name: str
    config_id: str
    base_config: str
    confidence: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    trace_id: str | None = None
    top_category: str | None = None
    top_summary: str | None = None


class RunHistoryListResponse(BaseModel):
    entries: list[RunHistoryEntry]
    total: int
    limit: int
    offset: int


@app.get("/api/runs/history", response_model=RunHistoryListResponse)
def list_run_history_endpoint(
    log_name: str | None = None,
    config_id: str | None = None,
    base_config: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> RunHistoryListResponse:
    """実行履歴をフィルタ付きで返す。新しい順。"""
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit は 1〜1000")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset は 0 以上")
    rows, total = storage.list_run_history(
        log_name=log_name or None,
        config_id=config_id or None,
        base_config=base_config or None,
        q=q or None,
        limit=limit,
        offset=offset,
    )
    return RunHistoryListResponse(
        entries=[RunHistoryEntry(**r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@app.get("/api/runs/history/{run_id}", response_model=RunHistoryEntry)
def get_run_history_endpoint(run_id: int) -> RunHistoryEntry:
    row = storage.get_run_history(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run id={run_id} not found")
    return RunHistoryEntry(**row)


@app.delete("/api/runs/history/{run_id}")
def delete_run_history_endpoint(run_id: int) -> dict:
    if not storage.delete_run_history(run_id):
        raise HTTPException(status_code=404, detail=f"run id={run_id} not found")
    return {"deleted": run_id}


# ─── 解析履歴 (完全再現用) ──────────────────────────────────────
# 設計: docs/plan/analysis_history.md


class AnalysisHistorySaveRequest(BaseModel):
    """``POST /api/analysis-history`` のリクエスト (config-log 解析完了時にフロントが送る)。"""

    run_id: str
    kind: str = "config-log"
    config_id: str
    analysis_mode: str | None = None
    single_source: str | None = None
    stage_order: str | None = None
    rally_max_rounds: int | None = None
    view_mode: str | None = None
    questionnaire_answers: dict[str, str] = {}
    topology: dict = {}
    result: dict


class AnalysisHistorySummary(BaseModel):
    """一覧用サマリ (重い request/result JSON は含まない)。"""

    id: int
    run_id: str
    created_at: str
    kind: str
    config_id: str
    analysis_mode: str | None = None
    single_source: str | None = None
    stage_order: str | None = None
    title: str | None = None
    confidence: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    top_category: str | None = None
    top_summary: str | None = None
    trace_id: str | None = None


class AnalysisHistoryListResponse(BaseModel):
    entries: list[AnalysisHistorySummary]
    total: int
    limit: int
    offset: int


def _summarize_analysis_result(result: dict) -> dict:
    """AnalysisResult dict から一覧用サマリ列を抽出する。"""
    cands = result.get("root_cause_candidates") or []
    top = cands[0] if cands else {}
    metrics = result.get("metrics") or {}
    top_summary = top.get("summary")
    return {
        "title": top_summary or None,
        "confidence": float(result.get("confidence", 0.0) or 0.0),
        "tokens_in": int(metrics.get("tokens_in", 0) or 0),
        "tokens_out": int(metrics.get("tokens_out", 0) or 0),
        "latency_ms": int(metrics.get("latency_ms_total", 0) or 0),
        "top_category": top.get("category"),
        "top_summary": top_summary,
        "trace_id": result.get("trace_id"),
    }


@app.post("/api/analysis-history")
def create_analysis_history_endpoint(req: AnalysisHistorySaveRequest) -> dict:
    """解析完了時の状態を 1 件保存する。同一 run_id は no-op (created=false)。"""
    summary = _summarize_analysis_result(req.result)
    request_payload = {
        "config_id": req.config_id,
        "analysis_mode": req.analysis_mode,
        "single_source": req.single_source,
        "stage_order": req.stage_order,
        "rally_max_rounds": req.rally_max_rounds,
        "view_mode": req.view_mode,
        "questionnaire_answers": req.questionnaire_answers,
        "topology": req.topology,
    }
    entry_id, created = storage.insert_analysis_history(
        run_id=req.run_id,
        kind=req.kind,
        config_id=req.config_id,
        analysis_mode=req.analysis_mode,
        single_source=req.single_source,
        stage_order=req.stage_order,
        title=summary["title"],
        confidence=summary["confidence"],
        tokens_in=summary["tokens_in"],
        tokens_out=summary["tokens_out"],
        latency_ms=summary["latency_ms"],
        top_category=summary["top_category"],
        top_summary=summary["top_summary"],
        trace_id=summary["trace_id"],
        request_json=json.dumps(request_payload, ensure_ascii=False),
        result_json=json.dumps(req.result, ensure_ascii=False),
    )
    return {"id": entry_id, "created": created}


@app.get("/api/analysis-history", response_model=AnalysisHistoryListResponse)
def list_analysis_history_endpoint(
    kind: str | None = None,
    analysis_mode: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> AnalysisHistoryListResponse:
    rows, total = storage.list_analysis_history(
        kind=kind, analysis_mode=analysis_mode, q=q, limit=limit, offset=offset
    )
    return AnalysisHistoryListResponse(
        entries=[AnalysisHistorySummary(**r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@app.get("/api/analysis-history/{entry_id}")
def get_analysis_history_endpoint(entry_id: int) -> dict:
    """個別取得 (request / result の完全 JSON 込み)。解析後画面の再現に使う。"""
    row = storage.get_analysis_history(entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"analysis-history id={entry_id} not found")
    return row


@app.delete("/api/analysis-history/{entry_id}")
def delete_analysis_history_endpoint(entry_id: int) -> dict:
    if not storage.delete_analysis_history(entry_id):
        raise HTTPException(status_code=404, detail=f"analysis-history id={entry_id} not found")
    return {"deleted": entry_id}


class AuditPromptResponse(BaseModel):
    prompt: str
    model: str


@app.get("/api/audit-prompt", response_model=AuditPromptResponse)
def get_audit_prompt() -> AuditPromptResponse:
    """GPT 監査エージェントの既定システムプロンプトを返す (UI の編集欄の初期値用)。"""
    from log_analyzer.audit_agent import SYSTEM_PROMPT, _DEFAULT_AUDIT_MODEL
    return AuditPromptResponse(
        prompt=SYSTEM_PROMPT,
        model=os.environ.get("AUDIT_MODEL") or _DEFAULT_AUDIT_MODEL,
    )


@app.get("/api/runtime-config", response_model=RuntimeConfigResponse)
def get_runtime_config() -> RuntimeConfigResponse:
    """ブラウザ UI が trace_id から Langfuse UI 直リンクを生成するのに使う。

    LANGFUSE_HOST が未設定 / 既定 localhost なら null を返し、UI 側は
    リンクではなくテキスト表示にフォールバックする。
    """
    host = os.environ.get("LANGFUSE_HOST", "").strip()
    if not host:
        return RuntimeConfigResponse(langfuse_host=None)
    return RuntimeConfigResponse(langfuse_host=host.rstrip("/"))


@app.get("/api/configs", response_model=ConfigsResponse)
def list_configs() -> ConfigsResponse:
    builtins: list[ConfigEntry] = [
        ConfigEntry(
            id=cid,
            label=BUILTIN_CONFIG_LABELS.get(cid, cid),
            type="builtin",
            base_config=cid,
        )
        for cid in CONFIG_RUNNERS.keys()
        if cid not in HIDDEN_BUILTIN_CONFIGS
    ]
    # 表示専用メタ構成 (例: config-log 解析のフロー)。
    # 単一実行タブで「構成図だけ確認したい」用途。実行ボタンは UI 側で無効化。
    view_only: list[ConfigEntry] = [
        ConfigEntry(
            id="config-log",
            label=BUILTIN_CONFIG_LABELS["config-log"],
            type="builtin_view_only",
            base_config="config-log",
        ),
    ]
    user_configs: list[ConfigEntry] = []
    for sc in storage.list_saved_configs():
        base = sc["base_config"]
        # config5（pipeline）派生は構造そのものをユーザーが設計しているのでベース表記不要、
        # config1〜4 派生は slot 上書きなのでベース構成が分かる方が選択時に判断しやすい
        if base == "config5":
            label = sc["name"]
        else:
            base_label = BUILTIN_CONFIG_LABELS.get(base, base)
            base_short = base_label.split("—", 1)[-1].split("（", 1)[0].split("(", 1)[0].strip()
            label = f"{sc['name']}（{base}: {base_short}）"
        user_configs.append(
            ConfigEntry(
                id=f"user:{sc['id']}",
                label=label,
                type="user",
                base_config=base,
            )
        )
    return ConfigsResponse(configs=builtins + view_only + user_configs)


@app.get("/api/logs", response_model=LogsResponse)
def list_logs() -> LogsResponse:
    if not _LOGS_DIR.exists():
        raise HTTPException(status_code=500, detail=f"logs directory not found: {_LOGS_DIR}")
    entries: list[LogEntry] = []
    for path in sorted(_LOGS_DIR.glob("*.log")):
        text = path.read_text(encoding="utf-8", errors="replace")
        stat = path.stat()
        entries.append(
            LogEntry(
                name=path.name,
                bytes=stat.st_size,
                lines=len(text.splitlines()),
                modified_at=datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            )
        )
    return LogsResponse(logs=entries)


@app.post("/api/logs", response_model=LogUploadResponse)
async def upload_log(file: UploadFile = File(...)) -> LogUploadResponse:
    """`.log` ファイルを samples/logs/ にアップロードする。

    ルール:
    - 拡張子は .log のみ
    - 同名既存があれば 409 で拒否（事故防止）
    - 10 MB を超えるファイルは 413 で拒否
    """
    if file.filename is None:
        raise HTTPException(status_code=400, detail="ファイル名が空です")
    target = _safe_log_path(file.filename)

    if target.exists():
        raise HTTPException(
            status_code=409,
            detail=f"同名のログが既に存在します: {file.filename}",
        )

    # サイズ制限はチャンクで読みつつ判定（メモリで丸ごと持たない）
    written = 0
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    try:
        with tmp_path.open("wb") as fh:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_LOG_SIZE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"ファイルサイズが上限を超えています "
                            f"({written} > {_MAX_LOG_SIZE_BYTES} bytes)"
                        ),
                    )
                fh.write(chunk)
        tmp_path.rename(target)
    except HTTPException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink()
        raise HTTPException(status_code=500, detail=f"アップロード失敗: {e}")

    text = target.read_text(encoding="utf-8", errors="replace")
    return LogUploadResponse(
        name=target.name,
        bytes=written,
        lines=len(text.splitlines()),
    )


@app.get("/api/logs/{name}/content", response_model=LogContentResponse)
def get_log_content(name: str) -> LogContentResponse:
    """ログ先頭 N 行（既定 200 行）をプレビュー用に返す。"""
    target = _safe_log_path(name)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"ログが見つかりません: {name}")
    text = target.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    preview = all_lines[:_PREVIEW_MAX_LINES]
    return LogContentResponse(
        name=name,
        bytes=target.stat().st_size,
        total_lines=len(all_lines),
        preview_lines=len(preview),
        truncated=len(all_lines) > len(preview),
        content="\n".join(preview),
    )


@app.delete("/api/logs/{name}")
def delete_log(name: str) -> dict:
    """ログファイルを削除。"""
    target = _safe_log_path(name)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"ログが見つかりません: {name}")
    try:
        target.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"削除失敗: {e}")
    return {"deleted": name}


class BuiltinStructureResponse(BaseModel):
    base_config: str
    nodes: list[dict]
    edges: list[dict]


@app.get("/api/configs/{base_config}/structure", response_model=BuiltinStructureResponse)
def get_builtin_structure(base_config: str) -> BuiltinStructureResponse:
    """builtin config1〜4 の固定グラフ構造を返す（React Flow 描画用）。"""
    if base_config not in BUILTIN_STRUCTURES:
        raise HTTPException(
            status_code=400,
            detail=f"no fixed structure for base_config={base_config}",
        )
    s = BUILTIN_STRUCTURES[base_config]
    return BuiltinStructureResponse(
        base_config=base_config,
        nodes=s["nodes"],
        edges=s["edges"],
    )


@app.get("/api/prompt-slots/{base_config}", response_model=PromptSlotsResponse)
def get_prompt_slots(base_config: str) -> PromptSlotsResponse:
    # view-only メタ構造 (例: config-log) は編集可能 slot を持たない
    if base_config == "config-log":
        return PromptSlotsResponse(base_config=base_config, slots=[])
    if base_config not in prompt_slots.VALID_BASE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"unknown base_config: {base_config}")
    slots = prompt_slots.get_slots(base_config)
    return PromptSlotsResponse(
        base_config=base_config,
        slots=[SlotInfo(**s) for s in slots],
    )


@app.get("/api/node-types", response_model=NodeTypesResponse)
def get_node_types() -> NodeTypesResponse:
    """構成5（user_pipeline）の UI で使えるノードタイプ定義を返す。"""
    return NodeTypesResponse(
        node_types=[NodeTypeDef(**nt) for nt in pipeline_runner.NODE_TYPE_DEFS],
        allowed_models=pipeline_runner.ALLOWED_MODELS,
    )


@app.get("/api/pipelines/default", response_model=PipelineDefaultResponse)
def get_default_pipeline() -> PipelineDefaultResponse:
    """新規 pipeline 作成時の出発点（input → output の最小構成）を返す。"""
    return PipelineDefaultResponse(
        pipeline={
            "nodes": [
                {"id": "input", "type": "input"},
                {
                    "id": "output",
                    "type": "output",
                    "prompt": pipeline_runner.DEFAULT_OUTPUT_PROMPT,
                    "model": "claude-sonnet-4-5",
                    "input_template": pipeline_runner.DEFAULT_INPUT_TEMPLATE_OUTPUT,
                },
            ],
            "edges": [{"source": "input", "target": "output"}],
        }
    )


@app.get("/api/configs/saved", response_model=SavedConfigsResponse)
def list_saved_configs_endpoint() -> SavedConfigsResponse:
    return SavedConfigsResponse(
        configs=[SavedConfigDTO(**sc) for sc in storage.list_saved_configs()]
    )


def _validate_overrides(base_config: str, prompt_overrides: dict[str, str], model_overrides: dict[str, str]) -> None:
    slots = prompt_slots.get_slots(base_config)
    valid_slot_ids = {s["slot_id"] for s in slots}
    invalid_p = [sid for sid in prompt_overrides if sid not in valid_slot_ids]
    if invalid_p:
        raise HTTPException(status_code=400, detail=f"unknown slot_id(s) for {base_config}: {invalid_p}")
    invalid_m = [sid for sid in model_overrides if sid not in valid_slot_ids]
    if invalid_m:
        raise HTTPException(status_code=400, detail=f"unknown model slot_id(s) for {base_config}: {invalid_m}")
    # 各 slot の allowed_models をチェック（empty なら model 上書き不可）
    by_id = {s["slot_id"]: s for s in slots}
    for sid, model_name in model_overrides.items():
        allowed = by_id[sid]["allowed_models"]
        if not allowed:
            raise HTTPException(status_code=400, detail=f"slot '{sid}' は model 上書き不可")
        if model_name not in allowed:
            raise HTTPException(status_code=400, detail=f"slot '{sid}' に許可されないモデル: {model_name}")


@app.post("/api/configs/saved", response_model=SavedConfigDTO)
def create_saved_config_endpoint(req: CreateSavedConfigRequest) -> SavedConfigDTO:
    if req.base_config not in prompt_slots.VALID_BASE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"unknown base_config: {req.base_config}")
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name は必須")
    if req.base_config == "config5":
        if not req.pipeline:
            raise HTTPException(status_code=400, detail="config5 は pipeline が必須")
        try:
            pipeline_runner.validate_pipeline(req.pipeline)
        except pipeline_runner.PipelineValidationError as e:
            raise HTTPException(status_code=400, detail=f"pipeline 検証失敗: {e}")
    else:
        _validate_overrides(req.base_config, req.overrides, req.model_overrides)
    try:
        saved = storage.create_saved_config(
            req.name, req.base_config, req.overrides, req.model_overrides, req.pipeline
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"保存失敗: {e}")
    return SavedConfigDTO(**saved)


@app.put("/api/configs/saved/{config_id}", response_model=SavedConfigDTO)
def update_saved_config_endpoint(config_id: int, req: UpdateSavedConfigRequest) -> SavedConfigDTO:
    existing = storage.get_saved_config(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"config id={config_id} not found")
    if existing["base_config"] == "config5":
        if not req.pipeline:
            raise HTTPException(status_code=400, detail="config5 は pipeline が必須")
        try:
            pipeline_runner.validate_pipeline(req.pipeline)
        except pipeline_runner.PipelineValidationError as e:
            raise HTTPException(status_code=400, detail=f"pipeline 検証失敗: {e}")
    else:
        _validate_overrides(existing["base_config"], req.overrides, req.model_overrides)
    saved = storage.update_saved_config(
        config_id, req.overrides, req.model_overrides, req.pipeline
    )
    if saved is None:
        raise HTTPException(status_code=404, detail=f"config id={config_id} not found")
    return SavedConfigDTO(**saved)


@app.delete("/api/configs/saved/{config_id}")
def delete_saved_config_endpoint(config_id: int) -> dict:
    deleted = storage.delete_saved_config(config_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"config id={config_id} not found")
    return {"deleted": config_id}


def _resolve_run_target(
    config: str,
    ad_hoc_prompt_overrides: dict[str, str] | None,
    ad_hoc_model_overrides: dict[str, str] | None,
    ad_hoc_pipeline: dict | None,
) -> tuple[str, dict[str, str], dict[str, str], dict | None]:
    """`config` 文字列を `(base_config, prompt_overrides, model_overrides, pipeline)` に解決。

    builtin: base_config はそのまま、上書き類は ad-hoc 値
    user:<id>: 保存済みから読み出す（ad-hoc は無視）
    """
    if config in CONFIG_RUNNERS:
        return (
            config,
            ad_hoc_prompt_overrides or {},
            ad_hoc_model_overrides or {},
            ad_hoc_pipeline,
        )
    if config.startswith("user:"):
        try:
            saved_id = int(config.split(":", 1)[1])
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid user config id: {config}")
        saved = storage.get_saved_config(saved_id)
        if saved is None:
            raise HTTPException(status_code=404, detail=f"saved config id={saved_id} not found")
        return (
            saved["base_config"],
            dict(saved["overrides"]),
            dict(saved.get("model_overrides", {})),
            saved.get("pipeline"),
        )
    raise HTTPException(status_code=400, detail=f"unknown config: {config}")


@app.post("/api/runs", response_model=AnalysisResult)
async def run_config(req: RunRequest) -> AnalysisResult:
    log_path = _LOGS_DIR / req.log_name
    if not log_path.exists() or log_path.suffix != ".log":
        raise HTTPException(status_code=404, detail=f"log not found: {req.log_name}")

    base_config, p_overrides, m_overrides, pipeline = _resolve_run_target(
        req.config, req.overrides, req.model_overrides, req.pipeline
    )

    if base_config == "config5":
        if not pipeline:
            raise HTTPException(status_code=400, detail="config5 は pipeline が必須")
        try:
            pipeline_runner.validate_pipeline(pipeline)
        except pipeline_runner.PipelineValidationError as e:
            raise HTTPException(status_code=400, detail=f"pipeline 検証失敗: {e}")
    else:
        if p_overrides or m_overrides:
            _validate_overrides(base_config, p_overrides, m_overrides)

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    runner = CONFIG_RUNNERS[base_config]

    loop = asyncio.get_running_loop()
    if base_config == "config5":
        result = await loop.run_in_executor(
            _executor,
            lambda: runner(
                log_text, str(log_path), pipeline_def=pipeline,
            ),
        )
    elif base_config == "config4":
        result = await loop.run_in_executor(
            _executor,
            lambda: runner(
                log_text,
                str(log_path),
                prompt_overrides=p_overrides,
                model_overrides=m_overrides,
                rally_max_rounds=req.rally_max_rounds,
            ),
        )
    else:
        result = await loop.run_in_executor(
            _executor,
            lambda: runner(
                log_text,
                str(log_path),
                prompt_overrides=p_overrides,
                model_overrides=m_overrides,
            ),
        )

    # 実行履歴を記録（失敗してもユーザーへの応答は妨げない）
    try:
        top = result.root_cause_candidates[0] if result.root_cause_candidates else None
        storage.insert_run_history(
            log_name=req.log_name,
            config_id=req.config,
            base_config=base_config,
            confidence=float(result.confidence),
            tokens_in=int(result.metrics.tokens_in),
            tokens_out=int(result.metrics.tokens_out),
            latency_ms=int(result.metrics.latency_ms_total),
            trace_id=str(result.trace_id),
            top_category=top.category.value if top else None,
            top_summary=top.summary if top else None,
        )
    except Exception:
        # 履歴記録は best-effort
        pass

    return result


# ─── SSE ストリーミング (config4 専用) ─────────────────────────────────


# {run_id: 確認モーダル応答待ち Future} を一時保持する。
# /api/runs/{run_id}/decision がここに値を set する。
_PENDING_DECISIONS: dict[str, asyncio.Future[dict]] = {}

# {run_id: 追加ログキュー} — 実行中に投入されたログを次の監視 / integrator が
# 取り込めるようにする。/api/runs/{run_id}/append-log が put、
# run_rally_stream が drain する。
_APPEND_QUEUES: dict[str, asyncio.Queue[dict]] = {}


def _sse_bytes(kind: str, data: dict) -> bytes:
    """1 イベントを SSE フォーマットでエンコード。"""
    return (
        f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


@app.post("/api/runs/stream")
async def runs_stream(req: RunRequest) -> StreamingResponse:
    """構成4 (rally) を SSE で 1 ステップずつ実行する。

    各ラウンドの ``monitor_decision`` イベントをリアルタイムで送る。
    ``rally_max_rounds`` を超えると ``await_confirmation`` を emit して停止し、
    ``POST /api/runs/{run_id}/decision`` が来るまで待機する。
    """
    log_path = _LOGS_DIR / req.log_name
    if not log_path.exists() or log_path.suffix != ".log":
        raise HTTPException(status_code=404, detail=f"log not found: {req.log_name}")

    base_config, p_overrides, m_overrides, _pipeline = _resolve_run_target(
        req.config, req.overrides, req.model_overrides, req.pipeline
    )
    if base_config != "config4":
        raise HTTPException(
            status_code=400,
            detail="ストリーミング実行は現在 config4 (rally) のみ対応",
        )
    if p_overrides or m_overrides:
        _validate_overrides(base_config, p_overrides, m_overrides)

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    run_id = uuid4().hex
    append_queue: asyncio.Queue[dict] = asyncio.Queue()
    _APPEND_QUEUES[run_id] = append_queue

    async def _wait_for_decision() -> dict:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        _PENDING_DECISIONS[run_id] = fut
        try:
            return await fut
        finally:
            _PENDING_DECISIONS.pop(run_id, None)

    async def gen() -> AsyncIterator[bytes]:
        yield _sse_bytes("run_id_assigned", {"run_id": run_id})
        final_data: dict | None = None
        try:
            async for ev in run_rally_stream(
                log_text,
                str(log_path),
                prompt_overrides=p_overrides,
                model_overrides=m_overrides,
                rally_max_rounds=req.rally_max_rounds or 3,
                decision_waiter=_wait_for_decision,
                append_queue=append_queue,
            ):
                yield _sse_bytes(ev.kind, ev.data)
                if ev.kind == "final":
                    final_data = ev.data.get("result")
        except Exception as e:
            yield _sse_bytes("error", {"message": str(e), "stage": "stream"})
            return
        finally:
            _APPEND_QUEUES.pop(run_id, None)

        # 履歴記録 (best-effort)
        if final_data is not None:
            try:
                cands = final_data.get("root_cause_candidates") or []
                top = cands[0] if cands else None
                metrics = final_data.get("metrics") or {}
                storage.insert_run_history(
                    log_name=req.log_name,
                    config_id=req.config,
                    base_config=base_config,
                    confidence=float(final_data.get("confidence", 0.0)),
                    tokens_in=int(metrics.get("tokens_in", 0)),
                    tokens_out=int(metrics.get("tokens_out", 0)),
                    latency_ms=int(metrics.get("latency_ms_total", 0)),
                    trace_id=str(final_data.get("trace_id") or ""),
                    top_category=(top or {}).get("category"),
                    top_summary=(top or {}).get("summary"),
                )
            except Exception:
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/runs/topology-stream")
async def runs_topology_stream(req: TopologyRunRequest) -> StreamingResponse:
    """トポロジー + ノード別ログを構成4 (rally) で SSE 実行する。

    リクエスト boundary は config4 / config4 派生 user 構成のみ。
    結果の AnalysisResult には ``suspected_node_ids`` が含まれ、UI 側で
    トポロジー上のノードをハイライト表示するのに使う。
    """
    base_config, p_overrides, m_overrides, _pipeline = _resolve_run_target(
        req.config, req.overrides, req.model_overrides, None
    )
    if base_config != "config4":
        raise HTTPException(
            status_code=400,
            detail="トポロジー解析は config4 (rally) ベースの構成のみ対応",
        )
    if p_overrides or m_overrides:
        _validate_overrides(base_config, p_overrides, m_overrides)

    log_text, normalized_nodes = _build_topology_log_text(
        req.topology, req.node_logs, req.node_configs,
        questionnaire_answers=req.questionnaire_answers,
    )
    if not normalized_nodes:
        raise HTTPException(status_code=400, detail="topology.nodes が空")
    # 少なくとも 1 ノードに 1 件以上の log または config が必要
    def _has_any_content(bucket: list[NodeAttachmentDTO] | None) -> bool:
        return bool(bucket) and any((a.content or "").strip() for a in bucket)
    has_any_attachment = any(
        _has_any_content(req.node_logs.get(n["id"]))
        or _has_any_content(req.node_configs.get(n["id"]))
        for n in normalized_nodes
    )
    if not has_any_attachment:
        raise HTTPException(
            status_code=400,
            detail="いずれか 1 ノード以上にログまたは設定ファイルを設定してください",
        )

    topology_context = {
        "nodes": normalized_nodes,
        "links": [
            {"source": s, "target": t}
            for s, t in (
                (lk.get("source"), lk.get("target"))
                for lk in (req.topology.get("links") or [])
                if isinstance(lk, dict)
            )
            if s and t
        ],
    }

    run_id = uuid4().hex
    append_queue: asyncio.Queue[dict] = asyncio.Queue()
    _APPEND_QUEUES[run_id] = append_queue
    log_ref = f"topology-run:{run_id[:8]}"

    async def _wait_for_decision() -> dict:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        _PENDING_DECISIONS[run_id] = fut
        try:
            return await fut
        finally:
            _PENDING_DECISIONS.pop(run_id, None)

    async def gen() -> AsyncIterator[bytes]:
        yield _sse_bytes("run_id_assigned", {"run_id": run_id})
        final_data: dict | None = None
        try:
            async for ev in run_rally_stream(
                log_text,
                log_ref,
                prompt_overrides=p_overrides,
                model_overrides=m_overrides,
                rally_max_rounds=req.rally_max_rounds or 3,
                decision_waiter=_wait_for_decision,
                append_queue=append_queue,
                topology_context=topology_context,
                audit_after_integrator=req.audit_after_integrator,
            ):
                yield _sse_bytes(ev.kind, ev.data)
                if ev.kind == "final":
                    final_data = ev.data.get("result")
        except Exception as e:
            yield _sse_bytes("error", {"message": str(e), "stage": "topology-stream"})
            return
        finally:
            _APPEND_QUEUES.pop(run_id, None)

        if final_data is not None:
            try:
                cands = final_data.get("root_cause_candidates") or []
                top = cands[0] if cands else None
                metrics = final_data.get("metrics") or {}
                storage.insert_run_history(
                    log_name=log_ref,
                    config_id=req.config,
                    base_config=base_config,
                    confidence=float(final_data.get("confidence", 0.0)),
                    tokens_in=int(metrics.get("tokens_in", 0)),
                    tokens_out=int(metrics.get("tokens_out", 0)),
                    latency_ms=int(metrics.get("latency_ms_total", 0)),
                    trace_id=str(final_data.get("trace_id") or ""),
                    top_category=(top or {}).get("category"),
                    top_summary=(top or {}).get("summary"),
                )
            except Exception:
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConfigLogRunRequest(BaseModel):
    """``POST /api/runs/config-log-stream`` のリクエスト。

    トポロジー + ノード別ログ + 設定ファイルを受け、config-log 解析を実行する。
    config4 (rally) ベースのみ対応。

    解析モードは ``analysis_mode`` で選ぶ:

    - ``"single"`` (1 段階): rally を 1 回だけ実行する。``single_source`` で
      どのデータを使うか選ぶ:
        - ``"config"``: 設定ファイルのみ (ログ不要)
        - ``"log"``:    ログのみ (設定不要)
        - ``"both"``:   設定 + ログを同時に投入 (既定)
    - ``"two_stage"`` (2 段階): Stage 1 で当たりをつけ、人間承認後に Stage 2 で
      検証する。``stage_order`` で順序を選ぶ:
        - ``"config_log"``: コンフィグ → (承認) → ログ
        - ``"log_config"``: ログ → (承認) → コンフィグ
    """

    config: str
    topology: dict
    node_logs: dict[str, list[NodeAttachmentDTO]] = {}
    node_configs: dict[str, list[NodeAttachmentDTO]] = {}
    questionnaire_answers: dict[str, str] = {}
    audit_after_integrator: bool = False
    audit_system_prompt: str | None = None  # GPT 監査プロンプトの上書き (空/None なら既定)
    rally_max_rounds: int | None = None
    overrides: dict[str, str] | None = None
    model_overrides: dict[str, str] | None = None
    # 解析モード
    analysis_mode: str = "single"          # "single" | "two_stage"
    single_source: str = "both"            # "config" | "log" | "both"  (analysis_mode=="single")
    stage_order: str = "config_log"        # "config_log" | "log_config" (analysis_mode=="two_stage")


_VALID_ANALYSIS_MODES = {"single", "two_stage"}
_VALID_SINGLE_SOURCES = {"config", "log", "both"}
_VALID_STAGE_ORDERS = {"config_log", "log_config"}


@app.post("/api/runs/config-log-stream")
async def runs_config_log_stream(req: ConfigLogRunRequest) -> StreamingResponse:
    """config-log 解析を SSE で実行する。

    1 段階モード (analysis_mode="single"): single_source に応じた合成ログで rally を 1 回。
    2 段階モード (analysis_mode="two_stage"): stage_order の順に Stage 1 → 人間承認 → Stage 2。
    """
    base_config, p_overrides, m_overrides, _pipeline = _resolve_run_target(
        req.config, req.overrides, req.model_overrides, None
    )
    if base_config != "config4":
        raise HTTPException(
            status_code=400,
            detail="config-log 解析は config4 (rally) ベースの構成のみ対応",
        )
    if p_overrides or m_overrides:
        _validate_overrides(base_config, p_overrides, m_overrides)

    # モード値の検証
    if req.analysis_mode not in _VALID_ANALYSIS_MODES:
        raise HTTPException(status_code=400, detail=f"analysis_mode は {_VALID_ANALYSIS_MODES} のいずれか")
    if req.analysis_mode == "single" and req.single_source not in _VALID_SINGLE_SOURCES:
        raise HTTPException(status_code=400, detail=f"single_source は {_VALID_SINGLE_SOURCES} のいずれか")
    if req.analysis_mode == "two_stage" and req.stage_order not in _VALID_STAGE_ORDERS:
        raise HTTPException(status_code=400, detail=f"stage_order は {_VALID_STAGE_ORDERS} のいずれか")

    # トポロジーの妥当性チェック (全モード共通)
    _, normalized_nodes = _build_topology_log_text(req.topology, {}, {})
    if not normalized_nodes:
        raise HTTPException(status_code=400, detail="topology.nodes が空")

    def _has_any_content(bucket) -> bool:
        if not bucket:
            return False
        return any((a.content or "").strip() for a in bucket)
    has_any_config = any(
        _has_any_content(req.node_configs.get(n["id"])) for n in normalized_nodes
    )
    has_any_log = any(
        _has_any_content(req.node_logs.get(n["id"])) for n in normalized_nodes
    )

    # 入力検証 (モードが要求するデータ種別が揃っているか):
    #   single+config / 2段階 config 始動 → Config が 1 件以上必要
    #   single+log    / 2段階 log 始動    → Log が 1 件以上必要
    #   single+both                       → Config または Log のいずれかが 1 件以上
    if req.analysis_mode == "single":
        if req.single_source == "config" and not has_any_config:
            raise HTTPException(status_code=400, detail="config のみモードでは少なくとも 1 ノードに設定ファイル (Config) が必要です")
        if req.single_source == "log" and not has_any_log:
            raise HTTPException(status_code=400, detail="log のみモードでは少なくとも 1 ノードにログが必要です")
        if req.single_source == "both" and not (has_any_config or has_any_log):
            raise HTTPException(status_code=400, detail="config+log モードでは少なくとも 1 ノードに設定ファイルまたはログが必要です")
    else:  # two_stage
        stage_one_kind = "config" if req.stage_order == "config_log" else "log"
        if stage_one_kind == "config" and not has_any_config:
            raise HTTPException(status_code=400, detail="config→log の 2 段階では Stage 1 用に少なくとも 1 ノードに設定ファイル (Config) が必要です")
        if stage_one_kind == "log" and not has_any_log:
            raise HTTPException(status_code=400, detail="log→config の 2 段階では Stage 1 用に少なくとも 1 ノードにログが必要です")

    # topology_context 共通
    topology_context = {
        "nodes": normalized_nodes,
        "links": [
            {"source": s, "target": t}
            for s, t in (
                (lk.get("source"), lk.get("target"))
                for lk in (req.topology.get("links") or [])
                if isinstance(lk, dict)
            )
            if s and t
        ],
    }

    run_id = uuid4().hex
    log_ref = f"config-log-run:{run_id[:8]}"

    async def _wait_for_decision() -> dict:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        _PENDING_DECISIONS[run_id] = fut
        try:
            return await fut
        finally:
            _PENDING_DECISIONS.pop(run_id, None)

    def _record_history(final_dict: dict) -> None:
        """最終 AnalysisResult を実行履歴へ記録する (best-effort)。"""
        try:
            cands = final_dict.get("root_cause_candidates") or []
            top = cands[0] if cands else None
            metrics = final_dict.get("metrics") or {}
            storage.insert_run_history(
                log_name=log_ref,
                config_id=req.config,
                base_config=base_config,
                confidence=float(final_dict.get("confidence", 0.0)),
                tokens_in=int(metrics.get("tokens_in", 0)),
                tokens_out=int(metrics.get("tokens_out", 0)),
                latency_ms=int(metrics.get("latency_ms_total", 0)),
                trace_id=str(final_dict.get("trace_id") or ""),
                top_category=(top or {}).get("category"),
                top_summary=(top or {}).get("summary"),
            )
        except Exception:
            pass

    # 各データ種別に対応する node_logs / node_configs を返すヘルパ
    #   "config" → configs のみ / "log" → logs のみ / "both" → 両方
    def _attachments_for(source: str) -> tuple[dict, dict]:
        logs = req.node_logs if source in ("log", "both") else {}
        configs = req.node_configs if source in ("config", "both") else {}
        return logs, configs

    # ─── 1 段階モード ───────────────────────────────────────────
    async def _gen_single() -> AsyncIterator[bytes]:
        from log_analyzer.rally_two_stage import (
            _result_to_stage_output, _build_final_result, _stage_label,
        )
        source = req.single_source
        logs, configs = _attachments_for(source)
        single_log_text, _ = _build_topology_log_text(
            req.topology, logs, configs,
            questionnaire_answers=req.questionnaire_answers,
        )
        stage_label = _stage_label(1, source)
        yield _sse_bytes("run_id_assigned", {"run_id": run_id})
        yield _sse_bytes(
            "single_stage_start",
            {
                "stage": source,
                "stage_ordinal": 1,
                "stage_label": stage_label,
                "source": source,
                "message": f"1 段階モード ({stage_label}) で rally を 1 回実行します。",
            },
        )
        final_result: AnalysisResult | None = None
        try:
            async for ev in run_rally_stream(
                single_log_text,
                f"{log_ref}::single",
                prompt_overrides=p_overrides,
                model_overrides=m_overrides,
                rally_max_rounds=req.rally_max_rounds or 3,
                decision_waiter=_wait_for_decision,
                topology_context={**topology_context, "stage": source},
                audit_after_integrator=req.audit_after_integrator,
                audit_system_prompt=req.audit_system_prompt,
            ):
                if "stage" not in ev.data:
                    ev.data["stage"] = source
                ev.data.setdefault("stage_ordinal", 1)
                if ev.kind == "final":
                    payload = ev.data.get("result")
                    if isinstance(payload, dict):
                        final_result = AnalysisResult.model_validate(payload)
                    # 上層には統合済 final を流すのでここでは流さない
                    continue
                yield _sse_bytes(ev.kind, ev.data)
        except Exception as e:
            yield _sse_bytes("error", {"message": str(e), "stage": "config-log-stream"})
            return

        if final_result is None:
            yield _sse_bytes("error", {"message": "1 段階 rally が結果を返しませんでした", "stage": source})
            return

        # 2 段階モードと同じ AnalysisResult 形式に寄せる (stage_outputs に 1 件のみ)
        stage_output = _result_to_stage_output(source, final_result, stage_label=stage_label)
        final = _build_final_result(
            stage_outputs=[stage_output],
            trace_id=final_result.trace_id,
            log_ref=log_ref,
        )
        final_dict = final.model_dump(mode="json")
        yield _sse_bytes("final", {"result": final_dict})
        _record_history(final_dict)

    # ─── 2 段階モード ───────────────────────────────────────────
    async def _gen_two_stage() -> AsyncIterator[bytes]:
        stage_one_kind = "config" if req.stage_order == "config_log" else "log"
        stage_two_kind = "log" if stage_one_kind == "config" else "config"

        # Stage 1: 始動データ種別だけで合成
        s1_logs, s1_configs = _attachments_for(stage_one_kind)
        stage_one_log_text, _ = _build_topology_log_text(
            req.topology, s1_logs, s1_configs,
            questionnaire_answers=req.questionnaire_answers,
        )

        # Stage 2: Stage 1 仮説を冒頭に置き、両データ種別を投入して検証
        def _stage_two_log_text(stage_one_output: StageOutput) -> str:
            from log_analyzer.rally_two_stage import _build_stage_one_hypothesis_block
            hypothesis_block = _build_stage_one_hypothesis_block(
                stage_one_output, source_kind=stage_one_kind, target_kind=stage_two_kind,
            )
            stage_two_body, _ = _build_topology_log_text(
                req.topology, req.node_logs, req.node_configs,
                questionnaire_answers=req.questionnaire_answers,
            )
            return hypothesis_block + stage_two_body

        yield _sse_bytes("run_id_assigned", {"run_id": run_id})
        final_data: dict | None = None
        try:
            async for ev in run_two_stage_stream(
                stage_one_log_text=stage_one_log_text,
                stage_two_log_text_template=_stage_two_log_text,
                log_ref=log_ref,
                topology_context=topology_context,
                stage_one_kind=stage_one_kind,
                stage_two_kind=stage_two_kind,
                prompt_overrides=p_overrides,
                model_overrides=m_overrides,
                rally_max_rounds=req.rally_max_rounds or 3,
                decision_waiter=_wait_for_decision,
                audit_after_integrator=req.audit_after_integrator,
                audit_system_prompt=req.audit_system_prompt,
            ):
                yield _sse_bytes(ev.kind, ev.data)
                if ev.kind == "final":
                    final_data = ev.data.get("result")
        except Exception as e:
            yield _sse_bytes("error", {"message": str(e), "stage": "config-log-stream"})
            return

        if final_data is not None:
            _record_history(final_data)

    chosen_gen = _gen_single if req.analysis_mode == "single" else _gen_two_stage
    return StreamingResponse(
        chosen_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/runs/{run_id}/append-log")
async def runs_append_log(run_id: str, req: AppendLogRequest) -> dict:
    """実行中のストリームに追加ログを投入する。

    キューに積まれたログは、次の監視 / integrator の実行開始時に rally_agent が
    drain し、以降のノードの動的入力ブロックに含める。元の log_text は変更しない
    （prompt caching を維持するため）。

    ``source`` がサンプルログ名（``*.log``）であれば samples/logs/ から実体を読み込み、
    ``content`` が空でもそのファイルの中身を投入する。両方指定された場合は
    ``content`` を優先する。
    """
    queue = _APPEND_QUEUES.get(run_id)
    if queue is None:
        raise HTTPException(
            status_code=404,
            detail=f"no active stream for run_id={run_id}",
        )
    content = (req.content or "").strip()
    source = (req.source or "inline").strip() or "inline"
    if not content and source.endswith(".log"):
        # samples/logs/ から読み込みのフォールバック
        try:
            target = _safe_log_path(source)
        except HTTPException as e:
            raise e
        if not target.exists():
            raise HTTPException(
                status_code=404, detail=f"log not found: {source}"
            )
        content = target.read_text(encoding="utf-8", errors="replace")
    if not content:
        raise HTTPException(status_code=400, detail="content または有効な source (.log) が必要")
    await queue.put({"source": source, "content": content})
    return {
        "ok": True,
        "run_id": run_id,
        "queued": queue.qsize(),
        "source": source,
        "bytes": len(content.encode("utf-8")),
    }


_VALID_DECISION_ACTIONS = {"continue", "stop", "advance", "abort"}


@app.post("/api/runs/{run_id}/decision")
async def runs_decision(run_id: str, req: DecisionRequest) -> dict:
    """確認モーダルからの応答。

    rally_max_rounds 上限到達時:
        - action="continue" (+ extend_by): rally_max_rounds を延長して再開
        - action="stop": 次ラウンドに進まず integrator にフォールバック

    config-log 解析の 2 段階モードで Stage 1 → Stage 2 遷移時:
        - action="advance": Stage 2 (検証) に進む
        - action="abort":   Stage 1 で打ち切り、Stage 1 結果のみを最終とする
    """
    # action の妥当性を先にチェック (pending decision の有無に依らず弾く)
    if req.action not in _VALID_DECISION_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"action は {sorted(_VALID_DECISION_ACTIONS)} のいずれか",
        )
    fut = _PENDING_DECISIONS.get(run_id)
    if fut is None or fut.done():
        raise HTTPException(
            status_code=404,
            detail=f"no pending decision for run {run_id}",
        )
    payload: dict = {"action": req.action}
    if req.action == "continue":
        extend_by = req.extend_by if (req.extend_by and req.extend_by > 0) else 3
        payload["extend_by"] = extend_by
    fut.set_result(payload)
    return {"ok": True, "run_id": run_id}


# ─── 問診票テンプレート CRUD (Phase B) ──────────────────────────


class QuestionnaireListResponse(BaseModel):
    templates: list[QuestionnaireTemplate]


class CreateQuestionnaireRequest(BaseModel):
    name: str
    description: str = ""
    items: list[QuestionnaireItem] = []


class UpdateQuestionnaireRequest(BaseModel):
    description: str | None = None  # None なら据置
    items: list[QuestionnaireItem] = []


@app.get("/api/questionnaires", response_model=QuestionnaireListResponse)
def list_questionnaires_endpoint() -> QuestionnaireListResponse:
    rows = storage.list_questionnaires()
    return QuestionnaireListResponse(
        templates=[QuestionnaireTemplate(**r) for r in rows]
    )


@app.get("/api/questionnaires/{qid}", response_model=QuestionnaireTemplate)
def get_questionnaire_endpoint(qid: int) -> QuestionnaireTemplate:
    row = storage.get_questionnaire(qid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"questionnaire id={qid} not found")
    return QuestionnaireTemplate(**row)


@app.post("/api/questionnaires", response_model=QuestionnaireTemplate)
def create_questionnaire_endpoint(req: CreateQuestionnaireRequest) -> QuestionnaireTemplate:
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name は必須")
    try:
        saved = storage.create_questionnaire(
            req.name, req.description, [it.model_dump() for it in req.items]
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"作成失敗: {e}")
    return QuestionnaireTemplate(**saved)


@app.put("/api/questionnaires/{qid}", response_model=QuestionnaireTemplate)
def update_questionnaire_endpoint(qid: int, req: UpdateQuestionnaireRequest) -> QuestionnaireTemplate:
    saved = storage.update_questionnaire(
        qid, req.description, [it.model_dump() for it in req.items]
    )
    if saved is None:
        raise HTTPException(status_code=404, detail=f"questionnaire id={qid} not found")
    return QuestionnaireTemplate(**saved)


@app.delete("/api/questionnaires/{qid}")
def delete_questionnaire_endpoint(qid: int) -> dict:
    try:
        ok = storage.delete_questionnaire(qid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"questionnaire id={qid} not found")
    return {"deleted": qid}
