"""Common output schema v0.1 — shared contract across all 4 configurations.

Every configuration (config1..config4) returns an `AnalysisResult` so results
can be compared mechanically. Do not break compatibility without bumping
`schema_version` and updating the comparison views.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

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


class AnalysisResult(BaseModel):
    schema_version: str = "v0.1"
    trace_id: UUID = Field(default_factory=uuid4)
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
