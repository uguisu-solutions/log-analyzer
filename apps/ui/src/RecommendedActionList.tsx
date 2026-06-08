/**
 * 推奨アクション表示（暫定対応 / 本質対応 グループ + グループ内 確信度降順 + 手順アコーディオン）。
 *
 * 各アクションはクリックで展開し、ジュニアエンジニアが着手できる手順・想定リスク・
 * ロールバック可否を表示する。config-log の結果ペイン / チャット結果 / 解析履歴詳細で共用。
 */
import type { RecommendedAction } from './types'

const GROUPS: Array<['provisional' | 'permanent', string]> = [
  ['provisional', '暫定対応'],
  ['permanent', '本質対応'],
]

function rollbackBadge(v?: string): { text: string; cls: string } {
  if (v === 'yes') return { text: 'ロールバック可', cls: 'rb-yes' }
  if (v === 'no') return { text: 'ロールバック不可', cls: 'rb-no' }
  return { text: 'ロールバック不明', cls: 'rb-unknown' }
}

export function RecommendedActionList({ actions }: { actions: RecommendedAction[] }) {
  return (
    <>
      {GROUPS.map(([kind, label]) => {
        const list = actions
          .filter(a => (kind === 'provisional' ? a.kind === 'provisional' : a.kind !== 'provisional'))
          .slice()
          .sort((x, y) => (y.confidence ?? 0) - (x.confidence ?? 0))  // グループ内 確信度降順
        if (list.length === 0) return null
        return (
          <div key={kind} className="action-group">
            <h5 className="action-group-title">{label}（{list.length}）</h5>
            <div className="action-list">
              {list.map((a, i) => <ActionItem key={i} a={a} />)}
            </div>
          </div>
        )
      })}
    </>
  )
}

function ActionItem({ a }: { a: RecommendedAction }) {
  const conf = a.confidence ?? 0
  const rb = rollbackBadge(a.rollback_possible)
  const steps = a.steps ?? []
  const risks = a.risks ?? []
  return (
    <details className="action-item">
      <summary className="action-summary">
        <span className="action-caret">▸</span>
        <span className="conf-badge" title="確信度">{conf.toFixed(2)}</span>
        <span className={`risk risk-${a.risk_level}`}>{a.risk_level}</span>
        {a.human_judgment_required && <span className="hjr-badge">人間判断必須</span>}
        <span className={`rb-badge ${rb.cls}`}>{rb.text}</span>
        <span className="action-text">{a.action}</span>
      </summary>
      <div className="action-detail">
        <div className="action-detail-title">手順（ジュニア向け）</div>
        {steps.length > 0 ? (
          <ol className="action-steps">
            {steps.map((s, i) => <li key={i}>{s}</li>)}
          </ol>
        ) : (
          <p className="muted small">手順は生成されていません。</p>
        )}

        <div className="action-detail-title">想定リスク</div>
        {risks.length > 0 ? (
          <ul className="action-risks">
            {risks.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        ) : (
          <p className="muted small">特記なし。</p>
        )}

        <div className="action-detail-title">ロールバック</div>
        <p className="action-rollback">
          <span className={`rb-badge ${rb.cls}`}>{rb.text}</span>
          {a.rollback_note ? <> — {a.rollback_note}</> : null}
        </p>
      </div>
    </details>
  )
}
