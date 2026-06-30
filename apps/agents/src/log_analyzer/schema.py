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
    """根本原因候補 1 件。

    schema v0.2 (2026-05-26) から ``rank`` フィールドを撤去。議事録
    「解析結果は複数（ランキング形式ではなく）表示する」に対応し、
    候補同士はフラットな並列として扱う。配列順は LLM 出力順を保持するが
    UI は順位を強調せず、並列カードとして表示する。
    """

    category: Category
    summary: str
    evidence: list[str] = Field(default_factory=list)


class RecommendedAction(BaseModel):
    action: str
    human_judgment_required: bool
    risk_level: RiskLevel
    # 対応の種別: "provisional"=暫定対応(応急処置) / "permanent"=本質対応(恒久対策)。
    # 旧データ互換のため既定は permanent。
    kind: str = "permanent"
    # このアクションの確信度 (0-1)。UI はグループ内でこの降順に表示する。
    confidence: float = 0.0
    # ジュニアエンジニアがそのまま着手できる粒度の実行手順 (順序付き)。
    steps: list[str] = Field(default_factory=list)
    # 手順を実施する際に想定されるリスク (アクション単位)。
    risks: list[str] = Field(default_factory=list)
    # ロールバック可否: "yes" | "no" | "unknown"。
    rollback_possible: str = "unknown"
    # ロールバック方法・補足。
    rollback_note: str = ""


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


class StageOutput(BaseModel):
    """config-log 解析の各 Stage の中間結果。

    - stage="config": コンフィグ情報で形成された仮説 / 結果
    - stage="log":    ログで形成された仮説 / 結果
    - stage="both":   1 段階モードで config + log を同時に投入した結果

    2 段階モードでは 2 件、1 段階モードでは 1 件。トポロジー解析タブ /
    config1-3 / config5 では空配列。
    """

    stage: str  # "config" | "log" | "both"
    stage_label: str = ""
    confidence: float = 0.0
    summary: str = ""
    suspected_node_ids: list[str] = Field(default_factory=list)
    suspected_node_findings: list[SuspectedNodeFinding] = Field(default_factory=list)
    delegation_rounds: int = 0
    delegation_history: list["DelegationEventDTO"] = Field(default_factory=list)
    trace_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms_total: int = 0
    root_cause_candidates: list[RootCauseCandidate] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    round_metrics: list["RoundMetrics"] = Field(default_factory=list)


class DelegationEventDTO(BaseModel):
    """構成4 委譲チェーンの 1 ステップを UI に渡すための DTO。

    kind の意味:
        - "orchestrator_initial":  オーケストレータが初手の監視を指名 (実行開始時 1 回)
        - "orchestrator_restart":  ユーザー介入により orchestrator が再選択 (2026-05-26 追加)
        - "monitor_delegation":    監視が次の監視に委譲
        - "monitor_finalize":      監視が integrator を指名（自然終了）
        - "routing_violation_fallback": 自己遷移 / ping-pong 違反で integrator に強制
        - "max_rounds_finalize":   rally_max_rounds 到達による強制 finalize
        - "user_finalize":         ユーザーが確認モーダルで停止を選択
        - "user_extend":           ユーザーが確認モーダルで延長を選択（履歴記録用）
    """

    round: int
    kind: str
    from_node: str | None = None  # "orchestrator" / "fw" / "routing" / ...
    to_node: str | None = None    # "fw" / "integrator" / ...
    focus_hint: str = ""
    rationale: str = ""
    confidence: float | None = None  # 監視の confidence（kind="monitor_*" のみ）


class RoundMetrics(BaseModel):
    """構成4 (rally) のラウンド単位集計 (Phase D)。

    議事録「ラウンド履歴、消費トークン、処理時間をラウンド単位で閲覧可能にする」
    に対応。各ラウンドで動いた監視ノード 1 件分の metrics を 1 行に持つ。

    role:
        - "orchestrator": round=0 (初手選択)
        - "<monitor>":    round>=1 (fw/routing/app/dns/sec 等)
        - "integrator":   round=最終
    """

    round: int
    role: str
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


class AuditReport(BaseModel):
    """GPT 監査エージェント (Phase C) の所見。

    Claude 系で動いた構成4 (rally) の結果を GPT-4o 系で独立検証する。
    議事録「監査エージェント (GPT想定)」に対応。

    verdict:
        - "agree":     Claude の結論に同意
        - "partial":   一部同意 (主原因は OK だが副次の指摘 / 抜けあり)
        - "disagree":  別の根本原因を主張
        - "uncertain": 与えられた情報では判断不能
    """

    verdict: str = "uncertain"
    confidence: float = 0.0
    summary: str = ""
    concerns: list[str] = Field(default_factory=list)
    alternative_hypotheses: list[str] = Field(default_factory=list)
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0


class QuestionnaireItem(BaseModel):
    """問診票の 1 設問 (Phase B)。

    type:
        - "text":   自由記述 1 行 (placeholder で例文を出せる)
        - "textarea": 自由記述 複数行
        - "choice": 選択式 (options 必須)
    """

    key: str
    label: str
    type: str = "text"  # "text" | "textarea" | "choice"
    options: list[str] = Field(default_factory=list)
    placeholder: str = ""
    required: bool = False


class QuestionnaireTemplate(BaseModel):
    """問診票テンプレート (Phase B)。

    SQLite ``questionnaire_templates`` テーブルに保存。デフォルトテンプレ
    (id=1, name='default') は init_db 時に自動投入する。
    """

    id: int
    name: str
    description: str = ""
    items: list[QuestionnaireItem] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class SourceSymbol(BaseModel):
    """ソースファイル内のシンボル 1 件（関数・クラス・メソッド・export）。

    本文は保持せず署名（名前・種別・行範囲）のみ。本文は read 時にディスクから読む
    （input トークン肥大を避けるオンデマンド前提。設計: docs/plan/source_code_analysis.md）。
    """

    name: str
    kind: str  # "function" | "class" | "method" | "export"
    start_line: int
    end_line: int


class SourceFile(BaseModel):
    """インデックス対象 1 ファイルの署名。"""

    path: str  # コードベースルートからの相対パス（POSIX 区切り）
    language: str  # "python" | "typescript" | "tsx" | "javascript" | ...
    bytes: int = 0
    lines: int = 0
    symbols: list[SourceSymbol] = Field(default_factory=list)


class DbColumn(BaseModel):
    name: str
    type: str = ""
    nullable: bool = True
    primary_key: bool = False
    default: str = ""
    foreign_key: str = ""  # "table.column" 形式。無ければ空


class DbTable(BaseModel):
    name: str
    columns: list[DbColumn] = Field(default_factory=list)
    indexes: list[list[str]] = Field(default_factory=list)  # 各 index の列名リスト
    # 抽出元: "ddl" | "orm/sqlalchemy" | "orm/django" | "orm/prisma"
    sources: list[str] = Field(default_factory=list)


class DbSchema(BaseModel):
    tables: list[DbTable] = Field(default_factory=list)


class SourceToolCall(BaseModel):
    """どの監視ノードが・どのラウンドで・何のソースを引いたかの記録（Phase 2/3）。

    再現と UI のノード別「参照したソース」表示に使う。
    """

    round: int = 0
    node: str = ""
    tool: str = ""  # "source_search" | "source_read" | "db_schema"
    args: dict[str, Any] = Field(default_factory=dict)
    result_chars: int = 0


class SourceContext(BaseModel):
    """ソースコード解析のコンテキスト（解析履歴の完全再現用）。"""

    codebase: str
    db_schema: DbSchema | None = None
    tool_calls: list[SourceToolCall] = Field(default_factory=list)
    total_chars_fetched: int = 0
    file_count: int = 0
    symbol_count: int = 0
    language_breakdown: dict[str, int] = Field(default_factory=dict)


def _default_trace_id() -> str:
    """trace_id の既定値: UUID4 を文字列で返す。

    通常は runner 側で Langfuse SDK が発行する trace.id を入れて上書きする。
    Langfuse なしで動作する単体テスト・CLI ローカル実行のためのフォールバック。
    """
    return str(uuid4())


class AnalysisResult(BaseModel):
    schema_version: str = "v0.2"
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
    # config-log 解析の各 Stage の中間結果。2 段階=2 件 / 1 段階=1 件 / 他構成=空配列。
    stage_outputs: list[StageOutput] = Field(default_factory=list)
    # 監査エージェント (Phase C) の所見。実行されなかった場合は None。
    audit_report: AuditReport | None = None
    # ラウンド単位集計 (Phase D)。token_log を round 順に並べたもの。
    # 他構成 (config1-3,5) では空のまま。
    round_metrics: list[RoundMetrics] = Field(default_factory=list)
    # ソースコード解析のコンテキスト。コードベース未指定の run では None。
    # 設計: docs/plan/source_code_analysis.md
    source_context: SourceContext | None = None


# 前方参照の解決 (StageOutput が DelegationEventDTO を文字列参照しているため)
StageOutput.model_rebuild()
