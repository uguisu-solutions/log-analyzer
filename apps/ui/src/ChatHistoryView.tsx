/**
 * チャット形式 結果表示 (Phase E)。
 *
 * 議事録「UI: チャット形式を想定し、問診票の要求や回答結果を表示する」に対応。
 * 既存の AnalysisResult を「人間 → orchestrator → 各監視 → integrator → 監査」の
 * 会話スレッドとしてレンダリングする読み取り専用ビュー。
 *
 * 入力データ:
 * - questionnaireAnswers: 一次申告 (Phase B)
 * - result.delegation_history: 委譲チェーン (Phase A)
 * - result.round_metrics: ラウンド別 tokens/latency (Phase D)
 * - result.root_cause_candidates / recommended_actions: 最終結論
 * - result.audit_report: GPT 監査 (Phase C)
 */
import type { AnalysisResult, DelegationEvent, QuestionnaireAnswers, RoundMetrics } from './types'

interface Props {
  result: AnalysisResult
  questionnaireAnswers: QuestionnaireAnswers
}

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

function findMetricForRole(metrics: RoundMetrics[], role: string, round?: number): RoundMetrics | null {
  if (round != null) {
    const exact = metrics.find(m => m.role === role && m.round === round)
    if (exact) return exact
  }
  return metrics.find(m => m.role === role) ?? null
}

export function ChatHistoryView({ result, questionnaireAnswers }: Props) {
  const messages: React.ReactNode[] = []
  const metrics = result.round_metrics

  // 1. 問診票回答 (人間メッセージ)
  const answerEntries = Object.entries(questionnaireAnswers).filter(([_, v]) => v.trim().length > 0)
  if (answerEntries.length > 0) {
    messages.push(
      <ChatMessage key="qa" sender="human" speaker="人間オペレータ" tag="問診票">
        <ul className="chat-qa-list">
          {answerEntries.map(([k, v]) => (
            <li key={k}><strong>{k}:</strong> {v}</li>
          ))}
        </ul>
      </ChatMessage>
    )
  } else {
    messages.push(
      <ChatMessage key="qa-skip" sender="human" speaker="人間オペレータ" tag="開始">
        <p className="muted">問診票未記入のまま解析を開始しました。</p>
      </ChatMessage>
    )
  }

  // 2. 委譲チェーン (orchestrator → 各監視 → integrator)
  const history: DelegationEvent[] = result.delegation_history ?? []
  history.forEach((d, i) => {
    if (d.kind === 'user_extend' || d.kind === 'user_finalize') {
      messages.push(
        <ChatMessage key={`ev-${i}`} sender="human" speaker="人間オペレータ" tag={d.kind === 'user_extend' ? '延長' : '停止'}>
          <p>{d.rationale}</p>
        </ChatMessage>
      )
      return
    }
    const fromRole = d.from_node ?? 'orchestrator'
    const toRole = d.to_node ?? 'integrator'
    const speakerRole = d.kind === 'orchestrator_initial' ? 'orchestrator' : fromRole
    const speaker = roleLabel(speakerRole)
    const m = findMetricForRole(metrics, speakerRole, d.round)
    messages.push(
      <ChatMessage key={`ev-${i}`} sender={speakerRole === 'integrator' ? 'integrator' : 'agent'} speaker={speaker} tag={`round ${d.round}`} metric={m}>
        <p className="chat-arrow"><strong>{roleLabel(fromRole)}</strong> → <strong>{roleLabel(toRole)}</strong></p>
        {d.rationale && <p className="chat-rationale">{d.rationale}</p>}
        {d.focus_hint && <p className="chat-focus"><em>次への観点: {d.focus_hint}</em></p>}
        {d.confidence != null && (
          <p className="chat-confidence muted">confidence {d.confidence.toFixed(2)}</p>
        )}
      </ChatMessage>
    )
  })

  // 3. integrator の最終結論
  const integratorMetric = findMetricForRole(metrics, 'integrator')
  messages.push(
    <ChatMessage
      key="integrator-final"
      sender="integrator"
      speaker="統合 (integrator)"
      tag="最終回答"
      metric={integratorMetric}
    >
      <p className="chat-confidence">確信度: <strong>{result.confidence.toFixed(2)}</strong></p>
      {result.root_cause_candidates.length > 0 && (
        <>
          <p className="chat-section-title">根本原因候補</p>
          <ul className="chat-causes">
            {result.root_cause_candidates.map((c, i) => (
              <li key={i}>
                <span className={`badge cat-${c.category}`}>{c.category}</span>
                {c.summary}
              </li>
            ))}
          </ul>
        </>
      )}
      {result.recommended_actions.length > 0 && (
        <>
          <p className="chat-section-title">推奨アクション</p>
          <ul className="chat-actions">
            {result.recommended_actions.map((a, i) => (
              <li key={i}>
                <span className={`risk risk-${a.risk_level}`}>{a.risk_level}</span>
                {a.human_judgment_required && <span className="hjr-badge">人間判断必須</span>}
                {a.action}
              </li>
            ))}
          </ul>
        </>
      )}
    </ChatMessage>
  )

  // 4. 監査エージェント (任意)
  if (result.audit_report) {
    const ar = result.audit_report
    messages.push(
      <ChatMessage
        key="audit"
        sender="audit"
        speaker="監査エージェント (GPT)"
        tag={`所見: ${ar.verdict}`}
      >
        {ar.summary && <p className="chat-rationale">{ar.summary}</p>}
        {ar.concerns.length > 0 && (
          <>
            <p className="chat-section-title">指摘事項</p>
            <ul className="chat-concerns">
              {ar.concerns.map((c, i) => <li key={i}>{c}</li>)}
            </ul>
          </>
        )}
        {ar.alternative_hypotheses.length > 0 && (
          <>
            <p className="chat-section-title">別の仮説</p>
            <ul className="chat-alternatives">
              {ar.alternative_hypotheses.map((h, i) => <li key={i}>{h}</li>)}
            </ul>
          </>
        )}
        <p className="muted chat-meta">
          model: {ar.model || 'gpt-4o-mini'} · confidence {ar.confidence.toFixed(2)} ·
          {' '}{ar.tokens_in.toLocaleString()}/{ar.tokens_out.toLocaleString()} tok ·
          {(ar.latency_ms / 1000).toFixed(1)}s
        </p>
      </ChatMessage>
    )
  }

  return <div className="chat-thread">{messages}</div>
}

export interface ChatMessageProps {
  sender: 'human' | 'agent' | 'integrator' | 'audit' | 'system'
  speaker: string
  tag?: string
  metric?: RoundMetrics | null
  children: React.ReactNode
}

export function ChatMessage({ sender, speaker, tag, metric, children }: ChatMessageProps) {
  return (
    <div className={`chat-message chat-${sender}`}>
      <div className="chat-avatar">
        {sender === 'human' ? '👤' : sender === 'integrator' ? '🧩' : sender === 'audit' ? '🔎' : sender === 'system' ? 'ℹ️' : '🤖'}
      </div>
      <div className="chat-bubble">
        <div className="chat-meta-row">
          <span className="chat-speaker">{speaker}</span>
          {tag && <span className="chat-tag">{tag}</span>}
          {metric && (
            <span className="chat-metric muted">
              {metric.model || ''} · {metric.tokens_in.toLocaleString()}/{metric.tokens_out.toLocaleString()} tok · {(metric.latency_ms / 1000).toFixed(1)}s
            </span>
          )}
        </div>
        <div className="chat-body">{children}</div>
      </div>
    </div>
  )
}
