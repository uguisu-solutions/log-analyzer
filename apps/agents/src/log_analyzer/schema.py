"""Common output schema v0.1 — shared contract across all 4 configurations.

Every configuration (config1..config4) returns an `AnalysisResult` so results
can be compared mechanically. Do not break compatibility without bumping
`schema_version` and updating the comparison views.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class Category(str, Enum):
    FW = "FW"
    NET = "Net"
    APP = "App"
    DNS = "DNS"
    SEC = "Sec"
    UNKNOWN = "Unknown"


class RiskLevel(str, Enum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


class ConfigId(str, Enum):
    CONFIG1 = "config1"
    CONFIG2 = "config2"
    CONFIG3 = "config3"
    CONFIG4 = "config4"
    CONFIG5 = "config5"


class RootCauseCandidate(BaseModel):
    rank: int = Field(ge=1)
    category: Category
    summary: str
    evidence: list[str] = Field(default_factory=list)


class RecommendedAction(BaseModel):
    action: str
    human_judgment_required: bool
    risk_level: RiskLevel


class TraceNode(BaseModel):
    node_id: str
    parent_id: str | None = None
    agent_name: str
    started_at: datetime
    ended_at: datetime | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)


class Metrics(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms_p50: int = 0
    latency_ms_total: int = 0
    compression_ratio: float = 0.0


class GraphNode(BaseModel):
    """構成のエージェント組織図上のノード（React Flow Canvas 描画用）。"""

    id: str
    label: str
    role: str  # "filter" | "model_call" | "triage" | "analyze" | "parallel_model" | "integrator" | "orchestrator" | "monitor"
    model: str | None = None
    latency_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """エージェント間のデータフローを示す有向辺。"""

    source: str
    target: str
    kind: str | None = None  # "forward" (既定) または "feedback"（再評価ループ）
    label: str | None = None


class SuspectedNodeFinding(BaseModel):
    """トポロジー解析タブで返す、障害候補ノード 1 件の詳細。

    severity の意味:
        - "primary":   その障害の直接原因と判断されたノード
        - "secondary": primary の影響で症状が出ているだけのノード
        - "info":      観測されたが障害に直接関与しないノード
    """

    node_id: str
    summary: str = ""
    severity: str = ""  # "primary" | "secondary" | "info" | ""


class DelegationEventDTO(BaseModel):
    """構成4 委譲チェーンの 1 ステップを UI に渡すための DTO。

    kind の意味:
        - "orchestrator_initial": オーケストレータが初手の監視を指名
        - "monitor_delegation":   監視が次の監視に委譲
        - "monitor_finalize":     監視が integrator を指名（自然終了）
        - "routing_violation_fallback": 自己遷移 / ping-pong 違反で integrator に強制
        - "max_rounds_finalize":  rally_max_rounds 到達による強制 finalize
        - "user_finalize":        ユーザーが確認モーダルで停止を選択
        - "user_extend":          ユーザーが確認モーダルで延長を選択（履歴記録用）
    """

    round: int
    kind: str
    from_node: str | None = None  # "orchestrator" / "fw" / "routing" / ...
    to_node: str | None = None    # "fw" / "integrator" / ...
    focus_hint: str = ""
    rationale: str = ""
    confidence: float | None = None  # 監視の confidence（kind="monitor_*" のみ）


def _default_trace_id() -> str:
    """trace_id の既定値: UUID4 を文字列で返す。

    通常は runner 側で Langfuse SDK が発行する trace.id を入れて上書きする。
    Langfuse なしで動作する単体テスト・CLI ローカル実行のためのフォールバック。
    """
    return str(uuid4())


class AnalysisResult(BaseModel):
    schema_version: str = "v0.1"
    # Langfuse が発行する trace ID（文字列）。UI のリンク生成と Langfuse UI 上の
    # 該当トレースを開く URL に直接使う。
    trace_id: str = Field(default_factory=_default_trace_id)
    config_id: ConfigId
    input_log_ref: str
    root_cause_candidates: list[RootCauseCandidate]
    recommended_actions: list[RecommendedAction]
    confidence: float = Field(ge=0.0, le=1.0)
    agent_trace: list[TraceNode] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)
    info_loss_flags: list[str] = Field(default_factory=list)
    # 後方互換（v0.1 のクライアントがこのフィールドを知らなくてもデコードできる）
    execution_graph_nodes: list[GraphNode] = Field(default_factory=list)
    execution_graph_edges: list[GraphEdge] = Field(default_factory=list)
    # 構成4（rally）専用。他構成では 0 / 空配列のまま
    delegation_rounds: int = 0
    delegation_max_rounds: int = 0
    delegation_history: list[DelegationEventDTO] = Field(default_factory=list)
    # トポロジー解析タブから実行された場合のみ埋まる。LLM が「障害に関係していると判断した」
    # ノードIDの部分集合。UI 側で該当ノードをハイライト表示する。
    # 他の経路（CLI / 通常 /api/runs / 構成1-3）では常に空配列。
    suspected_node_ids: list[str] = Field(default_factory=list)
    # 同上。ノード単位の詳細（summary / severity）。UI で各ノードに「ここで何が起こっているか」を
    # 表示するために使う。``suspected_node_ids`` と整合（ID は同じか部分集合）。
    suspected_node_findings: list[SuspectedNodeFinding] = Field(default_factory=list)
