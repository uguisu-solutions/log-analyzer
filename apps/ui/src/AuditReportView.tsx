/**
 * 監査エージェント所見の表示コンポーネント (Phase C)。
 *
 * GPT-4o-mini が Claude 系の結論を独立検証した結果を、結果ペインに
 * 配色付きで表示する。両タブ (トポロジー解析 / config-log 解析) で共用。
 */
import type { AuditReport } from './types'

const VERDICT_LABEL: Record<string, string> = {
  agree: '同意',
  partial: '部分同意',
  disagree: '反対',
  uncertain: '判断不能',
}

interface Props {
  report: AuditReport
}

export function AuditReportView({ report }: Props) {
  const verdict = report.verdict || 'uncertain'
  const label = VERDICT_LABEL[verdict] ?? verdict
  return (
    <section className={`audit-report audit-${verdict}`}>
      <div className="audit-header">
        <h4>監査エージェントの所見 (GPT)</h4>
        <span className={`audit-verdict-badge audit-${verdict}`}>{label}</span>
        <span className="audit-meta muted">
          model: {report.model || 'gpt-4o-mini'} ·
          confidence {report.confidence.toFixed(2)} ·
          {report.tokens_in.toLocaleString()}/{report.tokens_out.toLocaleString()} tok ·
          {(report.latency_ms / 1000).toFixed(1)}s
        </span>
      </div>
      {report.summary && <p className="audit-summary">{report.summary}</p>}
      {report.concerns.length > 0 && (
        <>
          <div className="audit-section-title">指摘事項 ({report.concerns.length})</div>
          <ul className="audit-concerns">
            {report.concerns.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </>
      )}
      {report.alternative_hypotheses.length > 0 && (
        <>
          <div className="audit-section-title">別の仮説 ({report.alternative_hypotheses.length})</div>
          <ul className="audit-alternatives">
            {report.alternative_hypotheses.map((h, i) => <li key={i}>{h}</li>)}
          </ul>
        </>
      )}
    </section>
  )
}
