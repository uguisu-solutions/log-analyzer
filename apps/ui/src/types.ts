export interface SlotInfo {
  slot_id: string
  label: string
  default_prompt: string
  default_model: string
  allowed_models: string[]  // 空配列ならモデル上書き不可
}

export interface ConfigEntry {
  id: string  // "config1" / "user:<id>"
  label: string
  type: 'builtin' | 'user'
  base_config: string
}

export interface SavedConfigDTO {
  id: number
  name: string
  base_config: string
  overrides: Record<string, string>
  model_overrides: Record<string, string>
  pipeline: PipelineDef | null
  created_at: string
  updated_at: string
}

// 構成5（user_pipeline）用のノード定義 ─────────────────────────

export type PipelineNodeType = 'input' | 'llm' | 'output'

export interface PipelineNode {
  id: string
  type: PipelineNodeType
  prompt?: string
  model?: string
  input_template?: string
  // React Flow 描画位置（保存時にも残す: UI で開いた時にレイアウトが復元される）
  position?: { x: number; y: number }
}

export interface PipelineEdge {
  source: string
  target: string
}

export interface PipelineDef {
  nodes: PipelineNode[]
  edges: PipelineEdge[]
}

export interface NodeTypeDef {
  type: PipelineNodeType
  label: string
  description: string
  fixed: boolean
  editable_fields: string[]
  default_prompt?: string | null
  default_model?: string | null
  default_input_template?: string | null
}

export interface NodeTypesResponse {
  node_types: NodeTypeDef[]
  allowed_models: string[]
}

// builtin config1〜4 の固定構造ノード（/api/configs/{base}/structure） ─────

export type BuiltinNodeKind = 'input' | 'slot' | 'slot_instance' | 'static'

export interface BuiltinStructureNode {
  id: string
  type: BuiltinNodeKind
  label: string
  slot_id?: string
  fixed_model?: string
}

export interface BuiltinStructureEdge {
  source: string
  target: string
  kind?: 'forward' | 'feedback'  // feedback はレイアウト除外 + 破線描画
  label?: string
}

export interface BuiltinStructureResponse {
  base_config: string
  nodes: BuiltinStructureNode[]
  edges: BuiltinStructureEdge[]
}

export interface LogEntry {
  name: string
  bytes: number
  lines: number
  modified_at: string  // ISO8601 (UTC)
}

export interface LogContent {
  name: string
  bytes: number
  total_lines: number
  preview_lines: number
  truncated: boolean
  content: string
}

export interface RunHistoryEntry {
  id: number
  started_at: string  // ISO8601 UTC
  log_name: string
  config_id: string
  base_config: string
  confidence: number | null
  tokens_in: number | null
  tokens_out: number | null
  latency_ms: number | null
  trace_id: string | null
  top_category: string | null
  top_summary: string | null
}

export interface RunHistoryListResponse {
  entries: RunHistoryEntry[]
  total: number
  limit: number
  offset: number
}

export interface RootCauseCandidate {
  rank: number
  category: string
  summary: string
  evidence: string[]
}

export interface RecommendedAction {
  action: string
  human_judgment_required: boolean
  risk_level: string
}

export interface GraphNodeData {
  id: string
  label: string
  role: string
  model?: string | null
  latency_ms?: number | null
  tokens_in?: number | null
  tokens_out?: number | null
  metadata?: Record<string, unknown>
}

export interface GraphEdgeData {
  source: string
  target: string
  kind?: string | null  // "forward" / "feedback" / null
  label?: string | null
}

export interface DelegationEvent {
  round: number
  // "orchestrator_initial" | "monitor_delegation" | "monitor_finalize"
  //  | "routing_violation_fallback" | "max_rounds_finalize"
  //  | "user_finalize" | "user_extend"
  kind: string
  from_node: string | null
  to_node: string | null
  focus_hint: string
  rationale: string
  confidence: number | null
}

// SSE ストリームで届くイベント。kind ごとに data が異なる。
export interface SSEEvent {
  kind: string
  data: Record<string, unknown>
}

export interface AnalysisResult {
  schema_version: string
  trace_id: string
  config_id: string
  input_log_ref: string
  root_cause_candidates: RootCauseCandidate[]
  recommended_actions: RecommendedAction[]
  confidence: number
  metrics: {
    tokens_in: number
    tokens_out: number
    cost_usd: number
    latency_ms_p50: number
    latency_ms_total: number
    compression_ratio: number
  }
  info_loss_flags: string[]
  execution_graph_nodes: GraphNodeData[]
  execution_graph_edges: GraphEdgeData[]
  // 構成4 専用（他構成では 0 / 空）
  delegation_rounds: number
  delegation_max_rounds: number
  delegation_history: DelegationEvent[]
  // トポロジー解析タブ専用（他経路では空配列）
  suspected_node_ids: string[]
  suspected_node_findings: SuspectedNodeFinding[]
}

export interface SuspectedNodeFinding {
  node_id: string
  summary: string
  severity: string  // "primary" | "secondary" | "info" | ""
}

// トポロジー解析タブで使うノード定義 ─────────────────────────
export interface TopologyNode {
  id: string
  type: string  // "L2" / "L3" / "FW" / "Server" / 任意
  label: string
  ip: string
  // 画像座標系での矩形（0..1 の正規化座標）
  x: number
  y: number
  w: number
  h: number
}

export interface TopologyLink {
  source: string
  target: string
}

export interface TopologyDef {
  image: string | null  // data URL (PNG/SVG)
  imageWidth: number
  imageHeight: number
  nodes: TopologyNode[]
  links: TopologyLink[]
}

// 1 ノードに添付する 1 ファイル (ログ or 設定ファイル) ─────────
export interface NodeAttachment {
  name: string
  content: string
}

// nodeId → 添付ファイル群
export type NodeAttachments = Record<string, NodeAttachment[]>
