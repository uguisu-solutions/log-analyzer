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
  // 実行の結末 (確認事項 B-4)。"ok" | "error" | "aborted" | "rejected"。
  // 対応前に記録された行は "ok"（正常終了しか記録していなかったため）。
  status?: string
  error_stage?: string | null
  error_message?: string | null
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
  // 再解析の系譜 (docs/plan/reanalysis.md)
  parent_run_id: string | null
  root_run_id: string | null
  revision: number
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
    questionnaire_confidences?: QuestionnaireConfidences
    topology: TopologyDef
    // 対象ファイル名のみ (本文は保存しない)。再解析画面での参照用。
    input_files?: string[]
    // BigQuery テーブル指定 (参照メタのみ)。再解析で持ち越す。
    node_bigquery?: NodeBigquerySources
  }
  result: AnalysisResult
}

// 再解析の種 (docs/plan/reanalysis.md)。解析履歴画面 → config-log 解析画面へ引き継ぐ。
// 前回の推論サマリと系譜情報を持ち、config-log 画面が受け取って再解析を開始する。
export interface ReanalyzeSeed {
  priorReasoning: string   // buildReasoningReport(前回result) の出力
  topology: TopologyDef    // 前回の構成図 (引き継ぎ)
  configId: string         // 前回の config_id
  parentRunId: string      // 直近の親 = 前回の run_id
  rootRunId: string        // 大元の run_id (前回が大元なら前回の run_id)
  revision: number         // 今回の世代 (= 前回 revision + 1)
  prevFiles: string[]      // 前回の対象ファイル名 (表示用)
  prevRevision: number     // 前回の世代 (表示用)
  // 前回の問診票入力 (再解析時にデフォルトとして引き継ぐ)
  prevQuestionnaire: QuestionnaireAnswers
  prevConfidences: QuestionnaireConfidences
  // 前回の BigQuery テーブル指定 (継続解析で持ち越す)
  prevBigquery: NodeBigquerySources
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
  questionnaire_confidences?: QuestionnaireConfidences
  topology: TopologyDef
  result: AnalysisResult
  // 再解析の系譜 (docs/plan/reanalysis.md)。初回解析は未指定。
  parent_run_id?: string | null
  root_run_id?: string | null
  revision?: number
  // 対象ファイル名のみ (本文は保存しない)
  input_files?: string[]
}

export interface RootCauseCandidate {
  category: string
  summary: string
  evidence: string[]
  // "supported"=主要候補 / "secondary"=副次要因 / "rejected"=棄却仮説 (旧データは未設定→主要扱い)
  status?: string
  // 旧 schema v0.1 互換: バックエンドが古いデータを返したときの保険。新規データには存在しない
  rank?: number
}

export interface RecommendedAction {
  action: string
  human_judgment_required: boolean
  risk_level: string
  // "provisional"=暫定対応(調査/切り分けを含む) / "permanent"=本質対応 (旧値 investigation は暫定扱い、旧データ未設定→本質対応)
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
  // 各監視ノードの調査根拠 (確認事項 A-3)。対応前に保存された履歴では undefined/空。
  monitor_reports?: MonitorReport[]
  // 解析方針の事前確認 (Phase 2) で承認された方針。確認ゲート未使用なら null/undefined。
  // focus_edited=true はユーザーが観点を修正して承認したことを示す。
  policy_proposal?: (PolicyProposal & { focus_edited?: boolean }) | null
  // ソースコード解析のコンテキスト。コードベース未指定なら null/undefined。
  source_context?: SourceContext | null
}

export interface SuspectedNodeFinding {
  node_id: string
  summary: string
  severity: string  // "primary" | "secondary" | "info" | ""
}

// ソースコード解析 (Phase 2/3) ─────────────────────────
export interface DbColumn {
  name: string
  type: string
  nullable: boolean
  primary_key: boolean
  default: string
  foreign_key: string  // "table.column" / ""
}

export interface DbTable {
  name: string
  columns: DbColumn[]
  indexes: string[][]
  sources: string[]  // "ddl" | "orm/sqlalchemy" | "orm/django" | "orm/prisma"
}

export interface DbSchema {
  tables: DbTable[]
}

// 監視ノードが行ったソース参照 1 件
export interface SourceToolCall {
  round: number
  node: string  // "fw" | "routing" | "app" | "dns" | "sec" | ""
  tool: string  // "source_search" | "source_read" | "db_schema"
  args: Record<string, unknown>
  result_chars: number
}

// 解析で使われたソースコードのコンテキスト (どのノードが何を参照したか)
export interface SourceContext {
  codebase: string
  db_schema: DbSchema | null
  tool_calls: SourceToolCall[]
  total_chars_fetched: number
  file_count: number
  symbol_count: number
  language_breakdown: Record<string, number>
}

// /api/source 一覧の 1 エントリ
export interface SourceCodebaseEntry {
  name: string
  file_count: number
  bytes: number
  symbol_count: number
  languages: Record<string, number>
  table_count: number
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

// 監視ノードの調査根拠 (確認事項 A-3) ─────────────────
// 監視 1 回ぶんの所見と、その根拠。(round, role) で delegation_history と対応する。
export interface MonitorFinding {
  category: string  // "FW" | "Net" | "App" | "DNS" | "Sec" | "Unknown" | ""
  summary: string
  evidence: string[]
}

export interface MonitorReport {
  round: number
  role: string  // "fw" | "routing" | "app" | "dns" | "sec"
  model: string
  confidence: number
  findings: MonitorFinding[]
  tool_calls: string[]
  rationale: string
  focus_hint_received: string
  focus_hint_for_next: string
  // 保存時に上限で切り詰めた場合の注記 (空なら全量保存)
  truncation_note: string
  parse_error: string | null
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
  // この Stage で動いた監視の調査根拠 (確認事項 A-3)
  monitor_reports?: MonitorReport[]
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

// 1 つの BigQuery テーブル指定 (type フィールドは持たない)。
// アップロードは常に有効なので、ここは「追加で BQ から取得するテーブル」を表す。
export interface NodeBqTable {
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

// nodeId → その節点に紐づく BQ テーブル群 (1 ノードに複数テーブル可)
export type NodeBigquerySources = Record<string, NodeBqTable[]>

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

// {key: '高'|'中'|'低'} の並列辞書。各申告の確信度 (人間が回答時に選ぶ)。任意。
export type QuestionnaireConfidences = Record<string, string>
export const CONFIDENCE_LEVELS = ['高', '中', '低'] as const

// ─── 解析評価 (解析レポート × 解答 の比較採点) ──────────────────
export interface AnswerScenario {
  scenario_key: string
  title: string
  trigger?: string
  initial_hypothesis?: string
  path?: string
  decision_points?: string
  evidence_source?: string
  conclusion?: string
  junior_pitfall?: string
  notes?: string
  source_file?: string
  imported_at?: string
}

export interface EvaluationDTO {
  id: number
  analysis_history_id: number
  scenario_key: string
  score: number | null
  axis_assessment: string[]
  good_points: string[]
  bad_points: string[]
  pitfalls_avoided: string[]
  pitfalls_hit: string[]
  summary: string
  model: string
  tokens_in: number | null
  tokens_out: number | null
  latency_ms: number | null
  created_at: string
}
