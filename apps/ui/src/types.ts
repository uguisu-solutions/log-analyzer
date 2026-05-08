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
  created_at: string
  updated_at: string
}

export interface LogEntry {
  name: string
  bytes: number
  lines: number
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
}
