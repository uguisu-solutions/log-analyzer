export interface SlotInfo {
  slot_id: string
  label: string
  default_prompt: string
  default_model: string
  allowed_models: string[]  // 空配列ならモデル上書き不可
}

export interface ConfigEntry {
  id: string  // "config1" / "user:<id>" / "config-log"
  label: string
  // "builtin":           単一実行 / 比較タブから実行可
  // "user":              saved_configs から作成されたユーザー定義
  // "builtin_view_only": 構成図表示のみ。実行は専用タブから
  type: 'builtin' | 'user' | 'builtin_view_only'
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

// 解析履歴 (完全再現用) — 設計: docs/plan/analysis_history.md ─────────
export interface AnalysisHistorySummary {
  id: number
  run_id: string
  created_at: string  // ISO8601 UTC
  kind: string
  config_id: string
  analysis_mode: string | null
  single_source: string | null
  stage_order: string | null
  title: string | null
  confidence: number | null
  tokens_in: number | null
  tokens_out: number | null
  latency_ms: number | null
  top_category: string | null
  top_summary: string | null
  trace_id: string | null
}

export interface AnalysisHistoryListResponse {
  entries: AnalysisHistorySummary[]
  total: number
  limit: number
  offset: number
}

// 詳細 (request / result 込み)。request.topology は TopologyDef 相当
export interface AnalysisHistoryDetail extends AnalysisHistorySummary {
  request: {
    config_id: string
    analysis_mode: string | null
    single_source: string | null
    stage_order: string | null
    rally_max_rounds: number | null
    view_mode: string | null
    questionnaire_answers: QuestionnaireAnswers
    topology: TopologyDef
  }
  result: AnalysisResult
}

// 解析履歴の保存リクエスト (config-log 完了時に送る)
export interface AnalysisHistorySaveRequest {
  run_id: string
  kind: string
  config_id: string
  analysis_mode: string | null
  single_source: string | null
  stage_order: string | null
  rally_max_rounds: number | null
  view_mode: string | null
  questionnaire_answers: QuestionnaireAnswers
  topology: TopologyDef
  result: AnalysisResult
}

export interface RootCauseCandidate {
  category: string
  summary: string
  evidence: string[]
  // 旧 schema v0.1 互換: バックエンドが古いデータを返したときの保険。新規データには存在しない
  rank?: number
}

export interface RecommendedAction {
  action: string
  human_judgment_required: boolean
  risk_level: string
  // "provisional"=暫定対応 / "permanent"=本質対応 (旧データは未設定→本質対応扱い)
  kind?: string
  confidence?: number          // このアクションの確信度 0-1 (グループ内で降順表示)
  steps?: string[]             // ジュニア向け実行手順 (順序付き)
  risks?: string[]             // 手順実施で想定されるリスク (アクション単位)
  rollback_possible?: string   // "yes" | "no" | "unknown"
  rollback_note?: string       // ロールバック方法・補足
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
  // config-log 解析専用（他経路では空配列）
  stage_outputs: StageOutput[]
  // 監査エージェント (Phase C) の所見。実行しなければ null
  audit_report: AuditReport | null
  // ラウンド単位集計 (Phase D)
  round_metrics: RoundMetrics[]
  // 解析方針の事前確認 (Phase 2) で承認された方針。確認ゲート未使用なら null/undefined。
  // focus_edited=true はユーザーが観点を修正して承認したことを示す。
  policy_proposal?: (PolicyProposal & { focus_edited?: boolean }) | null
}

export interface SuspectedNodeFinding {
  node_id: string
  summary: string
  severity: string  // "primary" | "secondary" | "info" | ""
}

// ラウンド単位集計 (Phase D) ─────────────────────────
export interface RoundMetrics {
  round: number
  role: string  // "orchestrator" | "<monitor>" | "integrator"
  model: string
  tokens_in: number
  tokens_out: number
  latency_ms: number
}

// 監査エージェント (Phase C) の所見 ─────────────────────
export interface AuditReport {
  verdict: string  // "agree" | "partial" | "disagree" | "uncertain"
  confidence: number
  summary: string
  concerns: string[]
  alternative_hypotheses: string[]
  model: string
  tokens_in: number
  tokens_out: number
  latency_ms: number
}

// config-log 解析の各 Stage 出力 ─────────────────────
export interface StageOutput {
  stage: string  // "config" | "log" | "both"
  stage_label: string
  confidence: number
  summary: string
  suspected_node_ids: string[]
  suspected_node_findings: SuspectedNodeFinding[]
  delegation_rounds: number
  delegation_history: DelegationEvent[]
  trace_id: string
  tokens_in: number
  tokens_out: number
  latency_ms_total: number
  root_cause_candidates: RootCauseCandidate[]
  recommended_actions: RecommendedAction[]
  round_metrics: RoundMetrics[]
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
  // ネットワーク構成図を Mermaid 記法で記述したもの (任意)。
  // AI 解析には log_text のテキスト文脈として渡す (パースはしない)。
  mermaid?: string
}

// 解析方針プランナー (Phase 2) がユーザー確認用に提案する方針。
// SSE `policy_proposal` の data.proposal として届く。
export interface PolicyProposal {
  situation_summary: string
  primary_hypotheses: string[]
  investigation_plan: string[]
  suggested_first_node: string  // "fw" | "routing" | "app" | "dns" | "sec"
  focus: string
  data_to_use: string[]
  missing_data_notes: string
  // 計測 (記録用、任意)
  model?: string
  tokens_in?: number
  tokens_out?: number
  latency_ms?: number
}

// 1 ノードに添付する 1 ファイル (ログ or 設定ファイル) ─────────
export interface NodeAttachment {
  name: string
  content: string
}

// nodeId → 添付ファイル群
export type NodeAttachments = Record<string, NodeAttachment[]>

// 1 ノードのログ取得元。'upload' = ファイルアップロード(従来) / 'bigquery' = BQ 取得
export interface NodeLogSource {
  type: 'upload' | 'bigquery'
  host?: string        // BQ 上の host 値 (空なら node id)
  table?: string       // テーブル名 (空なら環境変数の既定テーブル)
  // 列構成はテーブルごとに異なる前提。空欄でその絞り込みを無効化できる
  hostColumn?: string  // host で絞る列 (空ならテーブル全体 = 1 表 1 機器)
  timeColumn?: string  // 期間で絞る列 (空なら期間で絞らない)
  textColumn?: string  // キーワード検索する列 (空なら検索なし)
  columns?: string     // 取得列 (カンマ区切り。空なら全列)
  start?: string       // 取得既定の開始時刻 (ISO8601, 任意)
  end?: string         // 取得既定の終了時刻 (ISO8601, 任意)
  limit?: number       // 取得既定件数 (任意)
}

// nodeId → ログ取得元設定。未設定ノードは 'upload' 扱い
export type NodeLogSources = Record<string, NodeLogSource>

// 問診票 (Phase B) ─────────────────────────────────────
export interface QuestionnaireItem {
  key: string
  label: string
  type: string  // "text" | "textarea" | "choice"
  options: string[]
  placeholder: string
  required: boolean
}

export interface QuestionnaireTemplate {
  id: number
  name: string
  description: string
  items: QuestionnaireItem[]
  created_at: string
  updated_at: string
}

// {key: answer} の辞書
export type QuestionnaireAnswers = Record<string, string>
