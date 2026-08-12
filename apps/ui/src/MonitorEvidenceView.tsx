/**
 * 監視ノードの調査根拠の表示 (確認事項 A-3)。
 *
 * 従来、履歴に残るのは各監視の「委譲理由」と「次の観点」、および最終結論側の
 * 要約 (suspected_node_findings) だけで、**その監視が何を調べ、何を根拠に
 * そう判断したか**は Langfuse を開かないと追えなかった。
 * バックエンドが `AnalysisResult.monitor_reports` に findings / evidence /
 * tool_calls を保存するようになったため、シニアレビュー用に折りたたみで見せる。
 *
 * 委譲チェーン履歴 (DelegationHistoryView) とチャット表示 (ChatHistoryView) で共用。
 */
import type { MonitorReport } from './types'

/** (round, role) で対応する監視レポートを探す。無ければ null。 */
export function findMonitorReport(
  reports: MonitorReport[] | undefined,
  round: number | null | undefined,
  role: string | null | undefined,
): MonitorReport | null {
  if (!reports || reports.length === 0 || !role) return null
  if (round != null) {
    const exact = reports.find(r => r.round === round && r.role === role)
    if (exact) return exact
  }
  return reports.find(r => r.role === role) ?? null
}

const CATEGORY_FALLBACK = '所見'

interface Props {
  report: MonitorReport
  // 折りたたみの初期状態 (既定は閉じる)
  defaultOpen?: boolean
}

export function MonitorEvidenceView({ report, defaultOpen = false }: Props) {
  const findings = report.findings ?? []
  const toolCalls = report.tool_calls ?? []
  if (findings.length === 0 && toolCalls.length === 0 && !report.parse_error) return null

  const evidenceCount = findings.reduce((s, f) => s + (f.evidence?.length ?? 0), 0)

  return (
    <details className="monitor-evidence" open={defaultOpen}>
      <summary className="monitor-evidence-head">
        この監視が調べたこと
        <span className="muted small">
          （所見 {findings.length} 件 / 根拠 {evidenceCount} 件
          {toolCalls.length > 0 ? ` / ツール ${toolCalls.length} 回` : ''}）
        </span>
      </summary>

      {findings.length > 0 && (
        <ul className="monitor-findings">
          {findings.map((f, i) => (
            <li key={i} className="monitor-finding">
              <div className="monitor-finding-head">
                <span className={`badge cat-${f.category || 'Unknown'}`}>
                  {f.category || CATEGORY_FALLBACK}
                </span>
                <span className="monitor-finding-summary">{f.summary || '(要約なし)'}</span>
              </div>
              {(f.evidence?.length ?? 0) > 0 && (
                <ul className="monitor-evidence-list">
                  {f.evidence.map((e, j) => <li key={j}><code>{e}</code></li>)}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}

      {toolCalls.length > 0 && (
        <div className="monitor-tool-calls">
          <span className="monitor-subtitle">実行したツール</span>
          <ul>
            {toolCalls.map((c, i) => <li key={i}><code>{c}</code></li>)}
          </ul>
        </div>
      )}

      {report.focus_hint_received && (
        <p className="monitor-focus muted small">受け取った観点: {report.focus_hint_received}</p>
      )}
      {report.truncation_note && (
        <p className="monitor-truncation muted small">※ 保存時に一部省略: {report.truncation_note}</p>
      )}
      {report.parse_error && (
        <p className="monitor-parse-error small">⚠ この監視の JSON 解析に失敗: {report.parse_error}</p>
      )}
    </details>
  )
}

const ROLE_LABEL: Record<string, string> = {
  fw: 'FW 監視',
  routing: 'Routing 監視',
  app: 'App 監視',
  dns: 'DNS 監視',
  sec: 'Sec 監視',
}

interface SectionProps {
  reports: MonitorReport[] | undefined
  // 監視が 1 つ以上動いた解析かどうか。true で reports が空なら
  // 「保存されていません」の注記を出す (根拠保存の対応前に実行された履歴)。
  hasMonitorRuns?: boolean
}

/**
 * 標準表示 (結果ペイン) 用の一覧セクション。
 *
 * config-log 解析の標準表示には委譲チェーン履歴が無く、A-3 の置き場所が
 * 無かったため、結果ペインに「監視ノードの調査根拠」として独立して出す。
 */
export function MonitorEvidenceSection({ reports, hasMonitorRuns = true }: SectionProps) {
  const list = reports ?? []
  if (list.length === 0) {
    return hasMonitorRuns ? (
      <>
        <h4>監視ノードの調査根拠</h4>
        <MonitorEvidenceUnavailable />
      </>
    ) : null
  }
  return (
    <>
      <h4>監視ノードの調査根拠（{list.length} ノード）</h4>
      <div className="monitor-evidence-section">
        {list.map((r, i) => (
          <div key={i} className="monitor-evidence-entry">
            <div className="monitor-evidence-entry-head">
              <span className="round-num">round {r.round}</span>
              <strong>{ROLE_LABEL[r.role] ?? r.role}</strong>
              <span className="muted small">
                {r.model || ''}{r.confidence ? ` · conf ${r.confidence.toFixed(2)}` : ''}
              </span>
            </div>
            {r.rationale && <p className="monitor-entry-rationale">{r.rationale}</p>}
            <MonitorEvidenceView report={r} />
          </div>
        ))}
      </div>
    </>
  )
}

/** 監視根拠が 1 件も保存されていない解析向けの注記 (対応前の履歴)。 */
export function MonitorEvidenceUnavailable() {
  return (
    <p className="monitor-evidence-missing muted small">
      監視ごとの調査根拠（所見・根拠・実行ツール）は保存されていません。
      この解析は根拠保存の対応前に実行されたものです。
    </p>
  )
}
