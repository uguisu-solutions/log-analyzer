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
import type { QuestionnaireAnswers, SSEEvent } from './types'

interface Props {
  events: SSEEvent[]
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

export function LiveChatView({ events, questionnaireAnswers }: Props) {
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
            <li key={k}><strong>{k}:</strong> {v}</li>
          ))}
        </ul>
      </ChatMessage>
    )
  }

  events.forEach((ev, i) => {
    const d = ev.data
    const stageTag = d.stage === 'config' ? 'Stage 1' : d.stage === 'log' ? 'Stage 2' : undefined

    switch (ev.kind) {
      case 'run_id_assigned':
      case 'run_started':
        // 技術メタ。チャットには出さない
        return
      case 'stage_one_start':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="Stage 1 開始">
            <p>Configs 解析を開始します（人間承認モーダルが Stage 1 完了時に表示されます）。</p>
          </ChatMessage>
        )
        return
      case 'stage_one_skipped':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="Stage 1 skip">
            <p>{String(d.message ?? 'Configs 解析をスキップし、Logs のみで実行します。')}</p>
          </ChatMessage>
        )
        return
      case 'stage_one_complete':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="Stage 1 完了">
            <p>{String(d.message ?? 'Stage 1 が完了しました。承認モーダルを確認してください。')}</p>
          </ChatMessage>
        )
        return
      case 'stage_two_start':
        messages.push(
          <ChatMessage key={`ev-${i}`} sender="system" speaker="System" tag="Stage 2 開始">
            <p>Logs での事実確認 (Stage 2) を開始します。</p>
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
      case 'user_decision': {
        const action = String(d.action ?? '')
        const labelMap: Record<string, string> = {
          continue: '継続を選択',
          stop: '停止を選択',
          advance: 'Stage 2 に進む',
          abort: 'Stage 1 で終了',
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
            <p className="muted">独立検証中... ({String(d.model_hint ?? 'gpt-4o-mini')})</p>
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
