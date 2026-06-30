/**
 * 解析結果 (AnalysisResult) の「推論過程」を、人間が読みやすい **ノード(エージェント)毎の
 * Markdown レポート**に変換するユーティリティ。
 *
 * 委譲チェーン (delegation_history) を「どのノードが何をしたか」でグルーピングし、
 * 各ノードのラウンド・委譲先・理由・観点・confidence・モデル/トークン/レイテンシ
 * (round_metrics) を読みやすく並べる。2 段階解析では Stage ごとに展開する。
 */
import type {
  AnalysisResult,
  DelegationEvent,
  RoundMetrics,
  SourceToolCall,
  StageOutput,
} from './types'

const ROLE_LABEL: Record<string, string> = {
  orchestrator: 'オーケストレータ',
  integrator: '統合 (integrator)',
  fw: 'FW 監視',
  routing: 'Routing 監視',
  app: 'App 監視',
  dns: 'DNS 監視',
  sec: 'Sec 監視',
}
function roleLabel(role: string): string {
  return ROLE_LABEL[role] ?? `${role} 監視`
}

const SEVERITY_LABEL: Record<string, string> = {
  primary: '直接原因',
  secondary: '影響を受けた側',
  info: '参考',
}

// 委譲イベントを「行動の説明」に変換 (kind 別の日本語化)
function eventAction(d: DelegationEvent): string {
  const to = d.to_node ? roleLabel(d.to_node) : ''
  switch (d.kind) {
    case 'orchestrator_initial': return `初手に **${to}** を選択`
    case 'orchestrator_restart': return `介入を受けて **${to}** を再選択`
    case 'monitor_delegation': return `**${to}** に委譲`
    case 'monitor_finalize': return '統合 (integrator) を指名（自然終了）'
    case 'routing_violation_fallback': return '遷移制約違反のため integrator に強制フォールバック'
    case 'max_rounds_finalize': return 'ラウンド上限により強制終了'
    case 'user_finalize': return 'ユーザーが停止を選択'
    case 'user_extend': return 'ユーザーがラウンド延長を選択'
    default: return d.to_node ? `→ ${to}` : (d.kind || '')
  }
}

function actingRole(d: DelegationEvent): string {
  if (d.kind === 'orchestrator_initial' || d.kind === 'orchestrator_restart') return 'orchestrator'
  return d.from_node ?? 'orchestrator'
}

function metricFor(metrics: RoundMetrics[], role: string, round: number): RoundMetrics | undefined {
  return metrics.find(m => m.role === role && m.round === round) ?? metrics.find(m => m.role === role)
}

function metricSuffix(m: RoundMetrics | undefined): string {
  if (!m) return ''
  return ` _(model: ${m.model || '?'} · ${m.tokens_in.toLocaleString()}/${m.tokens_out.toLocaleString()} tok · ${(m.latency_ms / 1000).toFixed(1)}s)_`
}

interface StageBlock {
  label: string
  history: DelegationEvent[]
  metrics: RoundMetrics[]
  confidence: number
  candidates: AnalysisResult['root_cause_candidates']
  actions: AnalysisResult['recommended_actions']
}

function renderStage(lines: string[], st: StageBlock): void {
  // 委譲イベントを実行ノードごとにグルーピング (登場順を保つ)
  const order: string[] = []
  const byRole = new Map<string, DelegationEvent[]>()
  for (const d of st.history) {
    if (d.kind === 'user_finalize' || d.kind === 'user_extend') continue
    const role = actingRole(d)
    if (!byRole.has(role)) { byRole.set(role, []); order.push(role) }
    byRole.get(role)!.push(d)
  }

  for (const role of order) {
    lines.push(`### ${roleLabel(role)}`)
    for (const d of byRole.get(role)!) {
      const m = metricFor(st.metrics, role, d.round)
      lines.push(`- **round ${d.round}**: ${eventAction(d)}${metricSuffix(m)}`)
      if (d.confidence != null) lines.push(`    - confidence: ${d.confidence.toFixed(2)}`)
      if (d.rationale) lines.push(`    - 理由: ${d.rationale}`)
      if (d.focus_hint) lines.push(`    - 次への観点: ${d.focus_hint}`)
    }
    lines.push('')
  }

  // 統合ノードの最終結論
  const im = st.metrics.find(m => m.role === 'integrator')
  lines.push(`### ${roleLabel('integrator')}`)
  lines.push(`- 最終確信度: **${st.confidence.toFixed(2)}**${metricSuffix(im)}`)
  if (st.candidates.length > 0) {
    lines.push('- 根本原因候補:')
    for (const c of st.candidates) lines.push(`    - [${c.category}] ${c.summary}`)
  }
  const sortByConf = (xs: AnalysisResult['recommended_actions']) =>
    xs.slice().sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0))
  const rollbackText = (v?: string) =>
    v === 'yes' ? 'ロールバック可' : v === 'no' ? 'ロールバック不可' : 'ロールバック不明'
  const emitActions = (label: string, list: AnalysisResult['recommended_actions']) => {
    if (list.length === 0) return
    lines.push(`- 推奨アクション（${label}・確信度降順）:`)
    for (const a of list) {
      const flags = `[conf ${(a.confidence ?? 0).toFixed(2)}][${rollbackText(a.rollback_possible)}]`
      lines.push(`    - ${flags} ${a.action}`)
      if ((a.steps?.length ?? 0) > 0) {
        lines.push('        - 手順:')
        a.steps!.forEach((s, i) => lines.push(`            ${i + 1}. ${s}`))
      }
      if ((a.risks?.length ?? 0) > 0) lines.push(`        - 想定リスク: ${a.risks!.join(' / ')}`)
      if (a.rollback_note) lines.push(`        - ロールバック: ${a.rollback_note}`)
    }
  }
  emitActions('暫定対応', sortByConf(st.actions.filter(a => a.kind === 'provisional')))
  emitActions('本質対応', sortByConf(st.actions.filter(a => a.kind !== 'provisional')))
  lines.push('')
}

export function buildReasoningReport(result: AnalysisResult): string {
  const lines: string[] = []
  lines.push('# 推論過程レポート (config-log 解析)')
  lines.push('')
  lines.push('## 概要')
  lines.push(`- 最終確信度: **${result.confidence.toFixed(2)}**`)
  lines.push(`- 合計トークン (in / out): ${result.metrics.tokens_in.toLocaleString()} / ${result.metrics.tokens_out.toLocaleString()}`)
  lines.push(`- 合計レイテンシ: ${(result.metrics.latency_ms_total / 1000).toFixed(1)}s`)
  if (result.trace_id) lines.push(`- trace_id: ${result.trace_id}`)
  lines.push('')

  // 承認された解析方針 (Phase 2)。確認ゲートを使った場合のみ。
  const policy = result.policy_proposal
  if (policy) {
    lines.push('## 承認済み解析方針' + (policy.focus_edited ? '（観点修正あり）' : ''))
    if (policy.situation_summary) lines.push(`- 現象: ${policy.situation_summary}`)
    if ((policy.primary_hypotheses?.length ?? 0) > 0) {
      lines.push('- 想定原因:')
      for (const h of policy.primary_hypotheses) lines.push(`    - ${h}`)
    }
    if ((policy.investigation_plan?.length ?? 0) > 0) {
      lines.push('- 調査方針:')
      policy.investigation_plan.forEach((p, i) => lines.push(`    ${i + 1}. ${p}`))
    }
    if (policy.suggested_first_node) lines.push(`- 起点: ${policy.suggested_first_node}`)
    if (policy.focus) lines.push(`- 着目観点: ${policy.focus}`)
    if (policy.missing_data_notes) lines.push(`- 不足データ・前提: ${policy.missing_data_notes}`)
    if (policy.model) {
      const ti = (policy.tokens_in ?? 0).toLocaleString()
      const to = (policy.tokens_out ?? 0).toLocaleString()
      lines.push(`- _(model: ${policy.model} · ${ti}/${to} tok · ${((policy.latency_ms ?? 0) / 1000).toFixed(1)}s)_`)
    }
    lines.push('')
  }

  const stages: StageOutput[] = result.stage_outputs ?? []
  if (stages.length >= 2) {
    for (const s of stages) {
      lines.push(`## ${s.stage_label || s.stage}`)
      lines.push('')
      renderStage(lines, {
        label: s.stage_label || s.stage,
        history: s.delegation_history ?? [],
        metrics: s.round_metrics ?? [],
        confidence: s.confidence,
        candidates: s.root_cause_candidates ?? [],
        actions: s.recommended_actions ?? [],
      })
    }
  } else {
    lines.push('## 推論チェーン（ノード別）')
    lines.push('')
    renderStage(lines, {
      label: '',
      history: result.delegation_history ?? [],
      metrics: result.round_metrics ?? [],
      confidence: result.confidence,
      candidates: result.root_cause_candidates ?? [],
      actions: result.recommended_actions ?? [],
    })
  }

  // 障害候補ノード (トポロジ上のノード)
  if (result.suspected_node_findings && result.suspected_node_findings.length > 0) {
    lines.push('## 障害候補ノード')
    for (const f of result.suspected_node_findings) {
      const sev = SEVERITY_LABEL[f.severity || ''] ?? f.severity ?? ''
      lines.push(`- **${f.node_id}**${sev ? ` [${sev}]` : ''}: ${f.summary || '(詳細未記載)'}`)
    }
    lines.push('')
  }

  // GPT 監査
  if (result.audit_report) {
    const ar = result.audit_report
    lines.push('## GPT 監査')
    lines.push(`- verdict: **${ar.verdict}** (confidence ${ar.confidence.toFixed(2)}, model ${ar.model || '?'})`)
    lines.push(`- 消費トークン (in / out): ${ar.tokens_in.toLocaleString()} / ${ar.tokens_out.toLocaleString()} · ${(ar.latency_ms / 1000).toFixed(1)}s`)
    if (ar.summary) lines.push(`- 総評: ${ar.summary}`)
    if (ar.concerns.length > 0) {
      lines.push('- 指摘事項:')
      for (const c of ar.concerns) lines.push(`    - ${c}`)
    }
    if (ar.alternative_hypotheses.length > 0) {
      lines.push('- 別の仮説:')
      for (const h of ar.alternative_hypotheses) lines.push(`    - ${h}`)
    }
    lines.push('')
  }

  // 参照したソースコード (Phase 3)
  const sc = result.source_context
  if (sc) {
    lines.push('## 参照したソースコード')
    lines.push(
      `- コードベース: ${sc.codebase || '—'} ` +
      `(${sc.file_count} ファイル / ${sc.symbol_count} シンボル, ` +
      `取得 ${sc.total_chars_fetched.toLocaleString()} 文字 / ${sc.tool_calls.length} 回)`,
    )
    // ノード別にグルーピング
    const order: string[] = []
    const byNode = new Map<string, SourceToolCall[]>()
    for (const c of sc.tool_calls) {
      const k = c.node || '(不明)'
      if (!byNode.has(k)) { byNode.set(k, []); order.push(k) }
      byNode.get(k)!.push(c)
    }
    const target = (c: SourceToolCall): string => {
      const a = c.args ?? {}
      if (c.tool === 'source_search') return `検索「${String(a.query ?? '')}」`
      if (c.tool === 'source_read') return `取得 ${String(a.path ?? '')}${a.symbol ? ':' + String(a.symbol) : ''}`
      if (c.tool === 'db_schema') return `DBスキーマ ${a.table ? String(a.table) : '(全テーブル)'}`
      return c.tool
    }
    for (const node of order) {
      lines.push(`- **${roleLabel(node)}**`)
      for (const c of byNode.get(node)!) lines.push(`    - [r${c.round}] ${target(c)}`)
    }
    if (sc.db_schema && sc.db_schema.tables.length > 0) {
      lines.push(`- DB スキーマ: ${sc.db_schema.tables.map(t => t.name).join(', ')}`)
    }
    lines.push('')
  }

  return lines.join('\n')
}

/** テキストをファイルとしてダウンロードさせる (Markdown / プレーンテキスト)。 */
export function downloadText(filename: string, text: string, mime = 'text/markdown;charset=utf-8;'): void {
  const blob = new Blob(['﻿' + text], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
