/**
 * SSE で届く実行中ストリームをチャットメッセージに変換するライブビュー (Phase E 拡張)。
 *
 * 元の RealtimeStream は時系列の `event: kind` リスト。これは技術ログとしては正確だが
 * 「会話として読む」用途には向いていない。LiveChatView では各イベントを
 * 「誰が何を言ったか」のメッセージとして再構成し、解析の物語を追えるようにする。
 *
 * 完了後の結果は ChatHistoryView (静的) が引き継ぐ。LiveChatView は実行中の
 * リアルタイム表示専用。
 */
import { useEffect, useRef } from 'react'
import { ChatMessage } from './ChatHistoryView'
import type { QuestionnaireAnswers, QuestionnaireConfidences, SSEEvent } from './types'

interface Props {
  events: SSEEvent[]
  questionnaireAnswers: QuestionnaireAnswers
  questionnaireConfidences?: QuestionnaireConfidences
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

export function LiveChatView({ events, questionnaireAnswers, questionnaireConfidences }: Props) {
  const conf = questionnaireConfidences ?? {}
  const tailRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    tailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [events.length])

  const messages: React.ReactNode[] = []

  // 0. 問診票回答 (人間の最初のメッセージ)
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
  }

  events.forEach((ev, i) => {
    const d = ev.data
    // stage_ordinal はバックエンドが各イベントに注入する (1 / 2)。順序に依らず正しい Stage 番号を出す。
    const ord = (d as { stage_ordinal?: number }).stage_ordinal
    const stageTag = ord ? `Stage ${ord}` : undefined

    switch (ev.kind) {
      case 'run_id_assigned':
      case 'run_started':
        // 技術メタ。チャットには出さない
        return
      case 'single_stage_start':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="1 段階解析 開始">
            <p>{String(d.message ?? `${String(d.stage_label ?? '1 段階解析')} を開始します。`)}</p>
          </ChatMessage>
        )
        return
      case 'stage_one_start':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="Stage 1 開始">
            <p>{String(d.stage_label ?? 'Stage 1')} を開始します。</p>
          </ChatMessage>
        )
        return
      case 'stage_one_complete':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="Stage 1 完了">
            <p>{String(d.message ?? 'Stage 1 が完了しました。そのまま Stage 2 へ進みます。')}</p>
          </ChatMessage>
        )
        return
      case 'stage_two_start':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="Stage 2 開始">
            <p>{String(d.stage_label ?? 'Stage 2')}（事実確認）を開始します。</p>
            {d.prior_hypothesis_summary && (
              <p className="chat-rationale">前提仮説: {String(d.prior_hypothesis_summary)}</p>
            )}
          </ChatMessage>
        )
        return
      case 'orchestrator_start':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="agent" speaker="オーケストレータ" tag={stageTag ? `${stageTag} · 初手選択中` : '初手選択中'}>
            <p className="muted">初手の監視ノードを選択しています...</p>
          </ChatMessage>
        )
        return
      case 'orchestrator_decision':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="agent" speaker="オーケストレータ" tag={stageTag ? `${stageTag} · 初手` : '初手'}>
            <p className="chat-arrow">最初に <strong>{roleLabel(String(d.to_node ?? ''))}</strong> を呼びます</p>
            {d.focus_hint && <p className="chat-focus">観点: {String(d.focus_hint)}</p>}
            {d.rationale && <p className="chat-rationale">{String(d.rationale)}</p>}
          </ChatMessage>
        )
        return
      case 'monitor_start': {
        const role = String(d.node ?? '')
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="agent" speaker={roleLabel(role)} tag={`${stageTag ? stageTag + ' · ' : ''}round ${String(d.round)}`}>
            <p className="muted">分析中...</p>
            {d.focus_hint && <p className="chat-focus">観点: {String(d.focus_hint)}</p>}
          </ChatMessage>
        )
        return
      }
      case 'monitor_decision': {
        const fromRole = String(d.from_node ?? '')
        const toRole = String(d.to_node ?? '')
        const findings = (d.findings as Array<{ category: string; summary: string }>) ?? []
        const top = findings[0]
        const tokens_in = Number(d.tokens_in ?? 0)
        const tokens_out = Number(d.tokens_out ?? 0)
        const latency_ms = Number(d.latency_ms ?? 0)
        messages.push(
          <ChatMessage
            key={`ev-${i}`}
            sender="agent"
            speaker={roleLabel(fromRole)}
            tag={`${stageTag ? stageTag + ' · ' : ''}round ${String(d.round)}`}
            metric={{
              round: Number(d.round ?? 0),
              role: fromRole,
              model: String(d.model ?? ''),
              tokens_in,
              tokens_out,
              latency_ms,
            }}
          >
            <p className="chat-arrow">
              <strong>{roleLabel(fromRole)}</strong> → <strong>{roleLabel(toRole)}</strong>
              {d.confidence != null && <> (confidence {Number(d.confidence).toFixed(2)})</>}
            </p>
            {top && <p className="chat-rationale"><strong>{top.category}</strong>: {top.summary}</p>}
            {d.rationale && <p className="chat-focus">理由: {String(d.rationale)}</p>}
          </ChatMessage>
        )
        return
      }
      case 'log_appended': {
        const source = String(d.source ?? '?')
        // intervention:{type}:user の場合は type タグを綺麗に表示
        const m = source.match(/^intervention:(comment|log|config):/)
        const tagLabel = m
          ? (m[1] === 'comment' ? '介入コメント' : m[1] === 'log' ? '介入ログ' : '介入設定')
          : '追加ログ'
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="human" speaker="人間オペレータ" tag={tagLabel}>
            <p className="chat-rationale">＋ 投入 (source={source}, round_added={String(d.round_added ?? 0)})</p>
            {d.content && (
              <p className="muted small">{String(d.content).slice(0, 200)}{String(d.content).length > 200 ? '…' : ''}</p>
            )}
          </ChatMessage>
        )
        return
      }
      case 'intervention_restart':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="再選択">
            <p>ユーザー介入を検出 ({String(d.added_count ?? 0)} 件)。orchestrator に戻り初期ノードを再選択します。</p>
            {d.previous_planned_node && (
              <p className="muted small">予定していたノード: <code>{String(d.previous_planned_node)}</code></p>
            )}
          </ChatMessage>
        )
        return
      case 'await_confirmation':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="承認待ち">
            <p>rally_max_rounds={String(d.rally_max_rounds)} に到達しました。継続 / 停止 を選択してください（モーダル表示中）。</p>
          </ChatMessage>
        )
        return
      case 'policy_start':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="agent" speaker="方針プランナー" tag="方針立案中">
            <p className="muted">{String(d.message ?? '解析方針を立案しています…')}</p>
          </ChatMessage>
        )
        return
      case 'policy_proposal': {
        // 想定原因・不足データも出す (確認事項 A-1。履歴側の表示と項目を揃える)
        const pr = (d.proposal ?? {}) as {
          situation_summary?: string
          primary_hypotheses?: string[]
          investigation_plan?: string[]
          data_to_use?: string[]
          missing_data_notes?: string
          suggested_first_node?: string
          focus?: string
        }
        const hypotheses = pr.primary_hypotheses ?? []
        const plan = pr.investigation_plan ?? []
        const dataToUse = pr.data_to_use ?? []
        const missing = (pr.missing_data_notes ?? '').trim()
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="agent" speaker="方針プランナー" tag="方針提案（承認待ち）">
            {pr.situation_summary && <p className="chat-rationale">現象: {pr.situation_summary}</p>}
            {hypotheses.length > 0 && (
              <>
                <p className="chat-section-title">想定される原因の方向性</p>
                <ul className="chat-qa-list">
                  {hypotheses.map((h, j) => <li key={j}>{h}</li>)}
                </ul>
              </>
            )}
            {plan.length > 0 && (
              <>
                <p className="chat-section-title">調査方針</p>
                <ol className="chat-qa-list">
                  {plan.map((p, j) => <li key={j}>{p}</li>)}
                </ol>
              </>
            )}
            {dataToUse.length > 0 && (
              <>
                <p className="chat-section-title">使用するデータ</p>
                <ul className="chat-qa-list">
                  {dataToUse.map((x, j) => <li key={j}>{x}</li>)}
                </ul>
              </>
            )}
            {missing && (
              <>
                <p className="chat-section-title">不足データ・前提</p>
                <p className="chat-missing-data">{missing}</p>
              </>
            )}
            {pr.suggested_first_node && (
              <p className="chat-arrow">起点候補: <strong>{roleLabel(pr.suggested_first_node)}</strong></p>
            )}
            {pr.focus && <p className="chat-focus">観点: {pr.focus}</p>}
            <p className="muted small">この方針で進めるかをモーダルで確認してください。</p>
          </ChatMessage>
        )
        return
      }
      case 'policy_rejected':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="方針却下">
            <p className="chat-rationale" style={{ color: '#b3261e' }}>
              {String(d.message ?? '解析方針が却下されました。解析を中止します。')}
            </p>
          </ChatMessage>
        )
        return
      case 'user_decision': {
        const action = String(d.action ?? '')
        // 自動進行 (advance + auto) は人間の操作ではないので System メッセージで表示
        if (action === 'advance' && d.auto) {
          messages.push(
            <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="自動進行">
              <p>Stage 1 完了。 Stage 2 へ進みます。</p>
            </ChatMessage>
          )
          return
        }
        const labelMap: Record<string, string> = {
          continue: '継続を選択',
          stop: '停止を選択',
          approve_policy: '解析方針を承認',
          reject_policy: '解析方針を却下',
        }
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="human" speaker="人間オペレータ" tag="決定">
            <p>{labelMap[action] ?? action}{d.extend_by ? ` (+${String(d.extend_by)} ラウンド)` : ''}</p>
          </ChatMessage>
        )
        return
      }
      case 'integrator_start':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="integrator" speaker="統合 (integrator)" tag="統合中">
            <p className="muted">全監視結果を統合し最終回答を作成しています...</p>
          </ChatMessage>
        )
        return
      case 'integrator_done':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="integrator" speaker="統合 (integrator)" tag="統合完了">
            <p>確信度 <strong>{Number(d.confidence ?? 0).toFixed(2)}</strong> / 候補 {String(d.candidates ?? 0)} / アクション {String(d.actions ?? 0)}</p>
          </ChatMessage>
        )
        return
      case 'audit_start':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="audit" speaker="監査エージェント (GPT)" tag="検証中">
            <p className="muted">独立検証中... ({String(d.model_hint ?? 'gpt-5.5')})</p>
          </ChatMessage>
        )
        return
      case 'audit_done':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="audit" speaker="監査エージェント (GPT)" tag={`所見: ${String(d.verdict ?? '?')}`}>
            <p>監査完了: verdict={String(d.verdict ?? '?')} · 指摘 {String(d.concerns ?? 0)} 件 · 別案 {String(d.alternatives ?? 0)} 件</p>
            <p className="muted small">model: {String(d.model ?? '')} · {String(d.tokens_in ?? 0)}/{String(d.tokens_out ?? 0)} tok · {(Number(d.latency_ms ?? 0) / 1000).toFixed(1)}s</p>
          </ChatMessage>
        )
        return
      case 'max_rounds_finalize':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="強制終了">
            <p>rally_max_rounds 到達のため強制 finalize しました。</p>
          </ChatMessage>
        )
        return
      case 'final':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="完了">
            <p>解析が完了しました。下に最終結果が表示されます。</p>
          </ChatMessage>
        )
        return
      case 'error':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="エラー">
            <p className="chat-rationale" style={{ color: '#b3261e' }}>{String(d.message ?? 'unknown error')}</p>
          </ChatMessage>
        )
        return
      default:
        // 未知イベントはノイズ扱いで非表示
        return
    }
  })

  if (messages.length === 0) {
    return <div className="live-chat-empty muted">実行を開始するとここに会話形式で進行が表示されます。</div>
  }

  return (
    <div className="chat-thread live-chat">
      {messages}
      <div ref={tailRef} />
    </div>
  )
}
