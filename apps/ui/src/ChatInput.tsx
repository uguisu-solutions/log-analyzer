/**
 * 実行中の介入入力 UI (Phase E 拡張)。
 *
 * 議事録「処理中にプロンプトを追加で送れるように」「介入があった場合は、一度
 * オーケストレーションノードに戻り、初期ノード選択から再開」に対応。
 *
 * ユーザーが入力したコンテンツを POST /api/runs/{runId}/append-log で送信する。
 * バックエンド側で次ラウンド開始前に検出 → orchestrator を再起動する。
 *
 * 投入タイプ:
 *   - comment: 自然言語コメント (調査メモ / 仮説の補強)
 *   - log:     ログ行 (例: ユーザーが手元で取った tcpdump)
 *   - config:  設定ファイル抜粋
 */
import { useState } from 'react'

const API_BASE = 'http://localhost:8000'

type InterventionType = 'comment' | 'log' | 'config'

const TYPE_PLACEHOLDER: Record<InterventionType, string> = {
  comment: '解析エージェントへのメッセージや調査メモを記述...',
  log: 'ログ行を貼り付け (例: deny / connection refused 等)',
  config: '設定ファイル抜粋を貼り付け',
}

const TYPE_LABEL: Record<InterventionType, string> = {
  comment: 'コメント',
  log: 'ログ',
  config: '設定',
}

interface Props {
  runId: string | null
  disabled: boolean
}

export function ChatInput({ runId, disabled }: Props) {
  const [content, setContent] = useState('')
  const [type, setType] = useState<InterventionType>('comment')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastSent, setLastSent] = useState<string | null>(null)

  const canSend = !disabled && !sending && !!runId && content.trim().length > 0

  const send = async () => {
    if (!canSend) return
    setSending(true)
    setError(null)
    try {
      const source = `intervention:${type}:user`
      const r = await fetch(`${API_BASE}/api/runs/${runId}/append-log`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, source }),
      })
      if (!r.ok) {
        const text = await r.text()
        throw new Error(`HTTP ${r.status}: ${text.slice(0, 200)}`)
      }
      setLastSent(new Date().toLocaleTimeString('ja-JP'))
      setContent('')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSending(false)
    }
  }

  return (
    <div className={`chat-input ${disabled ? 'is-disabled' : ''}`}>
      <div className="chat-input-header">
        <span className="chat-input-title">介入を送信</span>
        <span className="chat-input-hint muted">
          送信すると orchestrator に戻り、初期ノードを再選択します
        </span>
      </div>
      <div className="chat-input-type">
        {(['comment', 'log', 'config'] as InterventionType[]).map(t => (
          <label key={t} className={`chat-input-type-pill ${type === t ? 'active' : ''}`}>
            <input
              type="radio"
              name="intervention-type"
              checked={type === t}
              onChange={() => setType(t)}
              disabled={disabled || sending}
            />
            <span>{TYPE_LABEL[t]}</span>
          </label>
        ))}
      </div>
      <textarea
        className="chat-input-text"
        value={content}
        onChange={e => setContent(e.target.value)}
        placeholder={runId ? TYPE_PLACEHOLDER[type] : '実行を開始すると入力できます'}
        rows={3}
        disabled={disabled || sending || !runId}
      />
      <div className="chat-input-actions">
        <button onClick={send} disabled={!canSend} className="chat-input-send">
          {sending ? '送信中…' : `${TYPE_LABEL[type]} を送信して再選択`}
        </button>
        {lastSent && !error && (
          <span className="chat-input-status muted">最終送信: {lastSent}</span>
        )}
        {error && <span className="chat-input-error">エラー: {error}</span>}
      </div>
    </div>
  )
}
