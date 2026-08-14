/**
 * 承認済み解析方針の読み取り専用表示 (確認事項 A-1 / A-2)。
 *
 * 方針プランナーが提案し、ユーザーが承認した方針 (`result.policy_proposal`) を
 * **解析後**にも確認できるようにする。解析前の確認モーダル
 * (`PolicyProposalModal`) が出していた全項目 — とりわけ
 * **想定原因 (primary_hypotheses)** と **不足データ・前提 (missing_data_notes)** —
 * は従来 UI に出しておらず、Markdown レポートを出力しないと追えなかった。
 *
 * 併せて方針プランナーの消費量 (model / tokens / latency) も表示する。
 * これは `policy_proposal` 内に保存されているが、本解析の
 * `metrics.tokens_in/out` には**含まれない**別枠の消費である。
 *
 * 使用箇所 (これら 2 つで「ライブ/履歴 × 標準/チャット」の 4 画面を賄う):
 *   - 標準表示: `CombinedResultView` (config-log 解析タブ・解析履歴詳細で共用)
 *   - チャット表示: `ChatHistoryView` の方針プランナー発言
 *
 * 方針ゲートを使わなかった解析では `policy_proposal` 自体が無いため非表示。
 */
import type { PolicyProposal } from './types'

type Policy = PolicyProposal & { focus_edited?: boolean }

const NODE_LABELS: Record<string, string> = {
  fw: 'FW 監視',
  routing: 'Routing 監視',
  app: 'App 監視',
  dns: 'DNS 監視',
  sec: 'Sec 監視',
}

export interface PlannerUsage {
  model: string
  tokensIn: number
  tokensOut: number
  latencyMs: number
}

/** 方針プランナーの消費量を取り出す。記録が無ければ null。 */
export function plannerUsage(policy: Policy | null | undefined): PlannerUsage | null {
  if (!policy) return null
  const usage: PlannerUsage = {
    model: policy.model ?? '',
    tokensIn: policy.tokens_in ?? 0,
    tokensOut: policy.tokens_out ?? 0,
    latencyMs: policy.latency_ms ?? 0,
  }
  // 旧い履歴には計測が入っていない場合がある
  if (!usage.model && !usage.tokensIn && !usage.tokensOut && !usage.latencyMs) return null
  return usage
}

/** プランナー消費量の 1 行表示 (A-2)。本解析の合計とは別枠である旨を明記する。 */
export function PlannerUsageLine({ policy }: { policy: Policy }) {
  const usage = plannerUsage(policy)
  if (!usage) return null
  return (
    <p className="policy-planner-usage muted small">
      方針プランナーの消費: {usage.model || 'モデル不明'} ·{' '}
      {usage.tokensIn.toLocaleString()} / {usage.tokensOut.toLocaleString()} tok ·{' '}
      {(usage.latencyMs / 1000).toFixed(1)}s
      <span className="policy-planner-note">（本解析の合計トークン／レイテンシには含まれません）</span>
    </p>
  )
}

interface Props {
  policy: Policy
  // 既定は閉じた状態。開いた状態で出したい画面では true を渡す。
  defaultOpen?: boolean
}

export function PolicySummaryView({ policy, defaultOpen = false }: Props) {
  const hypotheses = policy.primary_hypotheses ?? []
  const plan = policy.investigation_plan ?? []
  const dataToUse = policy.data_to_use ?? []
  const missing = (policy.missing_data_notes ?? '').trim()
  const firstNodeLabel = NODE_LABELS[policy.suggested_first_node] ?? policy.suggested_first_node

  return (
    <details className="policy-summary" open={defaultOpen}>
      <summary className="policy-summary-head">
        <strong>承認済み解析方針</strong>
        {policy.focus_edited && <span className="policy-edited-badge">観点修正あり</span>}
        <span className="muted small">
          （想定原因 {hypotheses.length} 件 / 調査方針 {plan.length} 件
          {missing ? ' · 不足データの指摘あり' : ''}）
        </span>
      </summary>

      <div className="policy-summary-body">
        {policy.situation_summary && (
          <div className="policy-section">
            <h4>現象の要約</h4>
            <p>{policy.situation_summary}</p>
          </div>
        )}

        {hypotheses.length > 0 && (
          <div className="policy-section">
            <h4>想定される原因の方向性</h4>
            <ul>
              {hypotheses.map((h, i) => <li key={i}>{h}</li>)}
            </ul>
          </div>
        )}

        {plan.length > 0 && (
          <div className="policy-section">
            <h4>調査の方針（起点と順序）</h4>
            <ol>
              {plan.map((p, i) => <li key={i}>{p}</li>)}
            </ol>
          </div>
        )}

        {policy.suggested_first_node && (
          <div className="policy-section">
            <h4>最初に当てる監視</h4>
            <p><strong>{firstNodeLabel}</strong></p>
          </div>
        )}

        {dataToUse.length > 0 && (
          <div className="policy-section">
            <h4>使用するデータ</h4>
            <ul>
              {dataToUse.map((d, i) => <li key={i}>{d}</li>)}
            </ul>
          </div>
        )}

        {missing && (
          <div className="policy-section policy-section-warn">
            <h4>不足データ・前提</h4>
            <p>{missing}</p>
          </div>
        )}

        {policy.focus && (
          <div className="policy-section">
            <h4>着目観点{policy.focus_edited ? '（ユーザーが修正）' : ''}</h4>
            <p>{policy.focus}</p>
          </div>
        )}

        <PlannerUsageLine policy={policy} />
      </div>
    </details>
  )
}
