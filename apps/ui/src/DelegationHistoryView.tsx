/**
 * 委譲チェーン履歴の表示コンポーネント。構成4 (rally) 結果と
 * トポロジー解析タブの両方から共用する。
 */
import type { AnalysisResult } from './types'

const DELEGATION_KIND_LABEL: Record<string, string> = {
  orchestrator_initial: 'orchestrator が初手を選択',
  monitor_delegation: '監視 → 監視 委譲',
  monitor_finalize: '監視 → integrator (自然終了)',
  routing_violation_fallback: '遷移制約違反 → integrator',
  max_rounds_finalize: 'rally_max_rounds 到達で強制 finalize',
  user_finalize: 'ユーザーが停止を選択',
  user_extend: 'ユーザーが延長を選択',
}

export function nodeLabel(name: string | null | undefined): string {
  if (!name) return '?'
  if (name === 'orchestrator') return 'orchestrator'
  if (name === 'integrator') return 'integrator'
  return `${name}_monitor`
}

export function DelegationHistoryView({ result }: { result: AnalysisResult }) {
  const rounds = result.delegation_rounds ?? 0
  const maxRounds = result.delegation_max_rounds ?? 0
  const history = result.delegation_history ?? []
  if (history.length === 0) return null
  const violations = history.filter(d => d.kind === 'routing_violation_fallback').length
  const extended = history.filter(d => d.kind === 'user_extend').length
  return (
    <section className="orchestrator-history">
      <h3>委譲チェーン履歴（{rounds} ラウンド / 上限 {maxRounds}）</h3>
      <div className="orchestrator-summary">
        <span className="kv">
          <span className="k">委譲ステップ</span>
          <span className="v">{history.length}</span>
        </span>
        {violations > 0 && (
          <span className="kv warn">
            <span className="k">⚠ 制約違反</span>
            <span className="v">{violations} 回 (integrator にフォールバック)</span>
          </span>
        )}
        {extended > 0 && (
          <span className="kv">
            <span className="k">ユーザー延長</span>
            <span className="v">{extended} 回</span>
          </span>
        )}
      </div>
      <ol className="orchestrator-rounds">
        {history.map((d, i) => (
          <li key={i} className={`orch-round action-${d.kind}`}>
            <div className="orch-round-header">
              <span className="round-num">round {d.round}</span>
              <span className={`action-badge action-${d.kind}`}>{d.kind}</span>
              {d.from_node && d.to_node && (
                <span className="invoke-list">
                  {nodeLabel(d.from_node)} → {nodeLabel(d.to_node)}
                </span>
              )}
              {d.confidence != null && (
                <span className="conf-pill">conf {d.confidence.toFixed(2)}</span>
              )}
            </div>
            <div className="orch-rationale">
              <strong>{DELEGATION_KIND_LABEL[d.kind] ?? d.kind}</strong>
              {d.rationale && <>: {d.rationale}</>}
            </div>
            {d.focus_hint && (
              <details className="focus-hints">
                <summary>focus_hint</summary>
                <p>{d.focus_hint}</p>
              </details>
            )}
          </li>
        ))}
      </ol>
    </section>
  )
}
