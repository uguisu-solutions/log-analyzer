/**
 * 解析方針の事前確認モーダル (Phase 2)。
 *
 * SSE で ``policy_proposal`` イベントを受けた config-log 解析タブが表示する。
 * 方針プランナーが提案した方針（現象要約・想定原因・調査方針・着目観点）を提示し、
 * ユーザーに承認 / 観点修正のうえ承認 / 中止 を選ばせる。
 *
 * 応答: POST /api/runs/{run_id}/decision
 *   {action: "approve_policy", edited_focus?: string}  → この方針で解析続行
 *   {action: "reject_policy"}                          → 方針を却下し中止
 */
import { useState } from 'react'
import type { PolicyProposal } from './types'

interface Props {
  proposal: PolicyProposal
  busy: boolean
  onApprove: (editedFocus: string | null) => void
  onReject: () => void
  // 再解析 (docs/plan/reanalysis.md): 前回解析からの続きの場合、前回の推論要約を提示する。
  priorReasoning?: string | null
}

const NODE_LABELS: Record<string, string> = {
  fw: 'FW 監視',
  routing: 'Routing 監視',
  app: 'App 監視',
  dns: 'DNS 監視',
  sec: 'Sec 監視',
}

export function PolicyProposalModal({ proposal, busy, onApprove, onReject, priorReasoning }: Props) {
  // 観点 (focus) はユーザーが修正してから承認できる
  const [focus, setFocus] = useState<string>(proposal.focus ?? '')
  const firstNodeLabel = NODE_LABELS[proposal.suggested_first_node] ?? proposal.suggested_first_node

  const focusEdited = focus.trim() !== (proposal.focus ?? '').trim()

  return (
    <div className="modal-overlay">
      <div className="modal policy-proposal-modal">
        <h3>解析方針の確認</h3>
        <p className="modal-summary">
          提供された構成図・ログ・設定・問診票から、以下の方針で障害解析を進めます。
          内容を確認し、この方針で進めるか（必要なら着目観点を修正して）選んでください。
        </p>

        {priorReasoning && priorReasoning.trim() && (
          <details className="policy-section policy-prior" open>
            <summary><strong>前回解析の要約（この再解析の起点）</strong></summary>
            <pre className="policy-prior-text">{priorReasoning.trim()}</pre>
          </details>
        )}

        {proposal.situation_summary && (
          <div className="policy-section">
            <h4>現象の要約</h4>
            <p>{proposal.situation_summary}</p>
          </div>
        )}

        {proposal.primary_hypotheses.length > 0 && (
          <div className="policy-section">
            <h4>想定される原因の方向性</h4>
            <ul>
              {proposal.primary_hypotheses.map((h, i) => <li key={i}>{h}</li>)}
            </ul>
          </div>
        )}

        {proposal.investigation_plan.length > 0 && (
          <div className="policy-section">
            <h4>調査の方針（起点と順序）</h4>
            <ol>
              {proposal.investigation_plan.map((p, i) => <li key={i}>{p}</li>)}
            </ol>
          </div>
        )}

        <div className="policy-section">
          <h4>最初に当てる監視</h4>
          <p><strong>{firstNodeLabel}</strong></p>
        </div>

        {proposal.data_to_use.length > 0 && (
          <div className="policy-section">
            <h4>使用するデータ</h4>
            <ul>
              {proposal.data_to_use.map((d, i) => <li key={i}>{d}</li>)}
            </ul>
          </div>
        )}

        {proposal.missing_data_notes && (
          <div className="policy-section policy-section-warn">
            <h4>不足データ・前提</h4>
            <p>{proposal.missing_data_notes}</p>
          </div>
        )}

        <div className="policy-section">
          <h4>着目観点（修正可）</h4>
          <textarea
            className="policy-focus-textarea"
            value={focus}
            rows={3}
            onChange={e => setFocus(e.target.value)}
            disabled={busy}
            placeholder="最初の監視に当てる観点。必要なら修正してください。"
          />
        </div>

        <div className="modal-actions">
          <button onClick={() => onApprove(focusEdited ? focus : null)} disabled={busy}>
            {focusEdited ? '観点を修正して解析' : 'この方針で解析'}
          </button>
          <button onClick={onReject} disabled={busy} className="btn-secondary">
            中止
          </button>
        </div>
      </div>
    </div>
  )
}
