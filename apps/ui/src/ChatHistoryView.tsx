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
import { MonitorEvidenceView, findMonitorReport } from './MonitorEvidenceView'
import { PlannerUsageLine } from './PolicySummaryView'
import { RecommendedActionList } from './RecommendedActionList'
import type {
  AnalysisResult,
  AuditReport,
  DelegationEvent,
  MonitorReport,
  QuestionnaireAnswers,
  QuestionnaireConfidences,
  RecommendedAction,
  RootCauseCandidate,
  RoundMetrics,
} from './types'

interface Props {
  result: AnalysisResult
  questionnaireAnswers: QuestionnaireAnswers
  // 各申告の確信度 (高/中/低)。任意。
  questionnaireConfidences?: QuestionnaireConfidences
  // 2 段階解析では stage_outputs を Stage ごとに展開する (既定 true)。
  // 単一 Stage を埋め込み描画する用途では false にして問診票ブロックを抑制できる。
  showQuestionnaire?: boolean
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

// 1 つの委譲チェーン + その最終結論を messages に積む。
// 2 段階解析では Stage ごとに呼び、key 衝突を避けるため keyPrefix を変える。
interface ConversationArgs {
  history: DelegationEvent[]
  metrics: RoundMetrics[]
  confidence: number
  candidates: RootCauseCandidate[]
  actions: RecommendedAction[]
  keyPrefix: string
  // 監視ごとの調査根拠 (確認事項 A-3)。対応前の履歴では空。
  reports: MonitorReport[]
}

function buildConversation(messages: React.ReactNode[], a: ConversationArgs): void {
  // 委譲チェーン (orchestrator → 各監視 → integrator)
  a.history.forEach((d, i) => {
    if (d.kind === 'user_extend' || d.kind === 'user_finalize') {
      messages.push(
        <ChatMessage key={`${a.keyPrefix}-ev-${i}`} sender="human" speaker="人間オペレータ" tag={d.kind === 'user_extend' ? '延長' : '停止'}>
          <p>{d.rationale}</p>
        </ChatMessage>
      )
      return
    }
    const fromRole = d.from_node ?? 'orchestrator'
    const toRole = d.to_node ?? 'integrator'
    const speakerRole = d.kind === 'orchestrator_initial' ? 'orchestrator' : fromRole
    const speaker = roleLabel(speakerRole)
    const m = findMetricForRole(a.metrics, speakerRole, d.round)
    // 監視の発言には、その監視の所見・根拠を折りたたみで添える (A-3)
    const report = speakerRole === 'orchestrator'
      ? null
      : findMonitorReport(a.reports, d.round, speakerRole)
    messages.push(
      <ChatMessage key={`${a.keyPrefix}-ev-${i}`} sender={speakerRole === 'integrator' ? 'integrator' : 'agent'} speaker={speaker} tag={`round ${d.round}`} metric={m}>
        <p className="chat-arrow"><strong>{roleLabel(fromRole)}</strong> → <strong>{roleLabel(toRole)}</strong></p>
        {d.rationale && <p className="chat-rationale">{d.rationale}</p>}
        {d.focus_hint && <p className="chat-focus"><em>次への観点: {d.focus_hint}</em></p>}
        {d.confidence != null && (
          <p className="chat-confidence muted">confidence {d.confidence.toFixed(2)}</p>
        )}
        {report && <MonitorEvidenceView report={report} />}
      </ChatMessage>
    )
  })

  // integrator の最終結論
  const integratorMetric = findMetricForRole(a.metrics, 'integrator')
  messages.push(
    <ChatMessage
      key={`${a.keyPrefix}-integrator-final`}
      sender="integrator"
      speaker="統合 (integrator)"
      tag="最終回答"
      metric={integratorMetric}
    >
      <p className="chat-confidence">確信度: <strong>{a.confidence.toFixed(2)}</strong></p>
      {a.candidates.length > 0 && (() => {
        const active = a.candidates.filter(c => c.status !== 'rejected')
        const rejected = a.candidates.filter(c => c.status === 'rejected')
        const renderItem = (c: RootCauseCandidate, i: number) => (
          <li key={i}>
            <span className={`badge cat-${c.category}`}>{c.category}</span>
            {c.status === 'secondary' && <span className="cand-status muted">（副次要因）</span>}
            {c.summary}
            {c.evidence && c.evidence.length > 0 && (
              <ul className="chat-evidence">
                {c.evidence.map((e, j) => <li key={j}>{e}</li>)}
              </ul>
            )}
          </li>
        )
        return (
          <>
            {active.length > 0 && (
              <>
                <p className="chat-section-title">根本原因候補</p>
                <ul className="chat-causes">{active.map(renderItem)}</ul>
              </>
            )}
            {rejected.length > 0 && (
              <>
                <p className="chat-section-title">棄却した仮説</p>
                <ul className="chat-causes chat-causes-rejected">{rejected.map(renderItem)}</ul>
              </>
            )}
          </>
        )
      })()}
      {a.actions.length > 0 && (
        <>
          <p className="chat-section-title">推奨アクション（クリックで手順を表示）</p>
          <RecommendedActionList actions={a.actions} />
        </>
      )}
    </ChatMessage>
  )
}

function buildAudit(messages: React.ReactNode[], ar: AuditReport): void {
  messages.push(
    <ChatMessage key="audit" sender="audit" speaker="監査エージェント (GPT)" tag={`所見: ${ar.verdict}`}>
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
        model: {ar.model || 'gpt-5.5'} · confidence {ar.confidence.toFixed(2)} ·
        {' '}{ar.tokens_in.toLocaleString()}/{ar.tokens_out.toLocaleString()} tok ·
        {(ar.latency_ms / 1000).toFixed(1)}s
      </p>
    </ChatMessage>
  )
}

export function ChatHistoryView({ result, questionnaireAnswers, questionnaireConfidences, showQuestionnaire = true }: Props) {
  const messages: React.ReactNode[] = []
  const conf = questionnaireConfidences ?? {}

  // 1. 問診票回答 (人間メッセージ)
  if (showQuestionnaire) {
    const answerEntries = Object.entries(questionnaireAnswers).filter(([_, v]) => v.trim().length > 0)
    if (answerEntries.length > 0) {
      messages.push(
        <ChatMessage key="qa" sender="human" speaker="人間オペレータ" tag="問診票">
          <ul className="chat-qa-list">
            {answerEntries.map(([k, v]) => (
              <li key={k}><strong>{k}:</strong> {v}{conf[k] ? <span className="qa-conf muted">（確信度: {conf[k]}）</span> : null}</li>
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
  }

  // 2. 承認された解析方針 (Phase 2)。確認ゲートを使った場合のみ。
  // 想定原因 (primary_hypotheses) と不足データ・前提 (missing_data_notes) も出す
  // (確認事項 A-1: 従来はチャット・標準とも非表示で、レポート出力でしか追えなかった)。
  const policy = result.policy_proposal
  if (policy) {
    const hypotheses = policy.primary_hypotheses ?? []
    const plan = policy.investigation_plan ?? []
    const dataToUse = policy.data_to_use ?? []
    const missing = (policy.missing_data_notes ?? '').trim()
    messages.push(
      <ChatMessage key="policy" sender="agent" speaker="方針プランナー" tag={policy.focus_edited ? '承認済み方針（観点修正あり）' : '承認済み方針'}>
        {policy.situation_summary && <p className="chat-rationale">現象: {policy.situation_summary}</p>}
        {hypotheses.length > 0 && (
          <>
            <p className="chat-section-title">想定される原因の方向性</p>
            <ul className="chat-qa-list">
              {hypotheses.map((h, i) => <li key={i}>{h}</li>)}
            </ul>
          </>
        )}
        {plan.length > 0 && (
          <>
            <p className="chat-section-title">調査方針</p>
            <ol className="chat-qa-list">
              {plan.map((p, i) => <li key={i}>{p}</li>)}
            </ol>
          </>
        )}
        {dataToUse.length > 0 && (
          <>
            <p className="chat-section-title">使用するデータ</p>
            <ul className="chat-qa-list">
              {dataToUse.map((d, i) => <li key={i}>{d}</li>)}
            </ul>
          </>
        )}
        {missing && (
          <>
            <p className="chat-section-title">不足データ・前提</p>
            <p className="chat-missing-data">{missing}</p>
          </>
        )}
        {policy.suggested_first_node && (
          <p className="chat-arrow">起点: <strong>{roleLabel(policy.suggested_first_node)}</strong></p>
        )}
        {policy.focus && <p className="chat-focus">観点: {policy.focus}</p>}
        {/* 方針プランナーの消費量 (確認事項 A-2)。本解析の metrics には含まれない別枠。 */}
        <PlannerUsageLine policy={policy} />
      </ChatMessage>
    )
  }

  const stages = result.stage_outputs ?? []
  if (stages.length >= 2) {
    // 2 段階解析: Stage ごとに推論過程 + その Stage の結論を展開する
    stages.forEach((st, si) => {
      messages.push(
        <div key={`stage-div-${si}`} className="chat-stage-divider">
          {st.stage_label || `Stage ${si + 1}`}
        </div>
      )
      buildConversation(messages, {
        history: st.delegation_history ?? [],
        metrics: st.round_metrics ?? [],
        confidence: st.confidence,
        candidates: st.root_cause_candidates ?? [],
        actions: st.recommended_actions ?? [],
        keyPrefix: `s${si}`,
        reports: st.monitor_reports ?? [],
      })
    })
  } else {
    // 単一 Stage / 1 段階モード: 従来どおり最終結果のチェーンを展開
    buildConversation(messages, {
      history: result.delegation_history ?? [],
      metrics: result.round_metrics,
      confidence: result.confidence,
      candidates: result.root_cause_candidates,
      actions: result.recommended_actions,
      keyPrefix: 'top',
      reports: result.monitor_reports ?? [],
    })
  }

  // 監査エージェント (任意。全体に対して 1 回)
  if (result.audit_report) buildAudit(messages, result.audit_report)

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
        {sender === 'human' ? 'You' : sender === 'integrator' ? 'INT' : sender === 'audit' ? 'AUD' : sender === 'system' ? 'SYS' : 'AGT'}
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
