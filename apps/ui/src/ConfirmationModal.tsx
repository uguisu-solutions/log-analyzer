/**
 * 確認モーダル: rally_max_rounds 到達時の継続/停止選択。
 *
 * SSE で ``await_confirmation`` イベントを受けたタブが表示する。
 * App.tsx / TopologyAnalysis.tsx / ConfigLogAnalysis.tsx 3 タブで共用。
 *
 * 応答: POST /api/runs/{run_id}/decision
 *   {action: "continue", extend_by: N}   → rally_max_rounds を +N 延長して再開
 *   {action: "stop"}                       → 即時 integrator にフォールバック
 */
import { useState } from 'react'
import { nodeLabel } from './DelegationHistoryView'
import type { DelegationEvent } from './types'

interface Props {
  round: number
  maxRounds: number
  history: DelegationEvent[]
  onContinue: (extendBy: number) => void
  onStop: () => void
  busy: boolean
}

export function ConfirmationModal({ round, maxRounds, history, onContinue, onStop, busy }: Props) {
  const [extendBy, setExtendBy] = useState<number>(3)
  return (
    <div className="modal-overlay">
      <div className="modal confirmation-modal">
        <h3>ラリーが上限に到達しました</h3>
        <p className="modal-summary">
          現在 <strong>{round}</strong> ラウンド完了、上限 <strong>{maxRounds}</strong>。
          委譲チェーンを継続するか、ここで integrator に進むかを選んでください。
        </p>
        <details className="modal-history" open>
          <summary>これまでの委譲履歴 ({history.length})</summary>
          <ol>
            {history.map((h, i) => (
              <li key={i} className={`mini-step kind-${h.kind}`}>
                <span className="mini-round">r{h.round}</span>
                <span className="mini-arrow">
                  {nodeLabel(h.from_node)} → {nodeLabel(h.to_node)}
                </span>
                {h.rationale && <span className="mini-rationale">{h.rationale}</span>}
              </li>
            ))}
          </ol>
        </details>
        <div className="modal-actions">
          <label className="extend-label">
            延長ラウンド数:
            <input
              type="number"
              min={1}
              max={10}
              value={extendBy}
              onChange={e => setExtendBy(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
              disabled={busy}
            />
          </label>
          <button onClick={() => onContinue(extendBy)} disabled={busy}>
            +{extendBy} 延長して継続
          </button>
          <button onClick={onStop} disabled={busy} className="btn-secondary">
            停止して integrator へ
          </button>
        </div>
      </div>
    </div>
  )
}
