/**
 * 解析結果 (AnalysisResult) の「推論過程」を CSV 文字列に変換するユーティリティ。
 *
 * ラウンド別評価シート用に round_metrics (round/role/model/tokens/latency) を
 * 主軸とし、対応する委譲イベント (delegation_history) から to_node / confidence /
 * rationale / focus_hint を突き合わせる。2 段階解析では stage_outputs を Stage ごとに展開。
 */
import type { AnalysisResult, DelegationEvent, RoundMetrics } from './types'

interface StageBlock {
  label: string
  history: DelegationEvent[]
  metrics: RoundMetrics[]
}

function csvCell(v: unknown): string {
  const s = v == null ? '' : String(v)
  // ダブルクォート・カンマ・改行を含む場合は引用符で囲む
  if (/[",\r\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"'
  return s
}

function eventRole(d: DelegationEvent): string {
  if (d.kind === 'orchestrator_initial') return 'orchestrator'
  return d.from_node ?? 'orchestrator'
}

export function buildReasoningCsv(result: AnalysisResult): string {
  const header = [
    'stage', 'round', 'role', 'to_node', 'model',
    'tokens_in', 'tokens_out', 'latency_ms', 'confidence', 'rationale', 'focus_hint',
  ]
  const rows: string[][] = [header]

  const stages: StageBlock[] =
    result.stage_outputs && result.stage_outputs.length >= 2
      ? result.stage_outputs.map(s => ({
          label: s.stage_label || s.stage,
          history: s.delegation_history ?? [],
          metrics: s.round_metrics ?? [],
        }))
      : [{ label: '', history: result.delegation_history ?? [], metrics: result.round_metrics ?? [] }]

  for (const st of stages) {
    if (st.metrics.length > 0) {
      // round_metrics を主軸に 1 行ずつ。対応する委譲イベントを round+role で突合
      for (const m of st.metrics) {
        const d =
          st.history.find(x => x.round === m.round && eventRole(x) === m.role) ??
          st.history.find(x => x.round === m.round)
        rows.push([
          st.label, String(m.round), m.role, d?.to_node ?? '', m.model ?? '',
          String(m.tokens_in ?? ''), String(m.tokens_out ?? ''), String(m.latency_ms ?? ''),
          d?.confidence != null ? d.confidence.toFixed(2) : '',
          d?.rationale ?? '', d?.focus_hint ?? '',
        ])
      }
    } else {
      // round_metrics が無い旧データは委譲履歴のみで出力
      for (const d of st.history) {
        rows.push([
          st.label, String(d.round ?? ''), eventRole(d), d.to_node ?? '', '',
          '', '', '', d.confidence != null ? d.confidence.toFixed(2) : '',
          d.rationale ?? '', d.focus_hint ?? '',
        ])
      }
    }
  }

  return rows.map(r => r.map(csvCell).join(',')).join('\r\n')
}

/** CSV 文字列をファイルとしてダウンロードさせる (Excel 互換のため BOM 付き)。 */
export function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
