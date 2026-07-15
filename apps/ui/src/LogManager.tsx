import { useState, useRef } from 'react'
import type { LogEntry, LogContent } from './types'

import { API_BASE, apiFetch } from './api'

interface Props {
  logs: LogEntry[]
  onLogsChange: () => Promise<unknown>  // 一覧を再取得するコールバック
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleString('ja-JP', { hour12: false })
  } catch {
    return iso
  }
}

export function LogManager({ logs, onLogsChange }: Props) {
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [previewing, setPreviewing] = useState<string | null>(null)
  const [previewContent, setPreviewContent] = useState<LogContent | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [deletingName, setDeletingName] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const clearMessages = () => {
    setError(null)
    setInfo(null)
  }

  const handleUpload = async (file: File) => {
    clearMessages()
    if (!file.name.endsWith('.log')) {
      setError(`拡張子は .log のみ対応: ${file.name}`)
      return
    }
    if (file.size > 10 * 1024 * 1024) {
      setError(`ファイルサイズが上限 10 MB を超えています: ${formatBytes(file.size)}`)
      return
    }
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const r = await apiFetch(`${API_BASE}/api/logs`, {
        method: 'POST',
        body: fd,
      })
      if (!r.ok) {
        const detail = await r.text()
        let msg = `HTTP ${r.status}`
        try {
          const j = JSON.parse(detail)
          msg = j.detail ?? msg
        } catch { /* not JSON */ }
        throw new Error(msg)
      }
      const data = await r.json()
      setInfo(`アップロード完了: ${data.name}（${data.lines} 行 / ${formatBytes(data.bytes)}）`)
      await onLogsChange()
      if (fileInputRef.current) fileInputRef.current.value = ''
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setUploading(false)
    }
  }

  const handlePreview = async (name: string) => {
    clearMessages()
    setPreviewing(name)
    setPreviewLoading(true)
    setPreviewContent(null)
    setPreviewError(null)
    try {
      const r = await apiFetch(`${API_BASE}/api/logs/${encodeURIComponent(name)}/content`)
      if (!r.ok) {
        const detail = await r.text()
        let msg = `HTTP ${r.status}`
        try {
          const j = JSON.parse(detail)
          msg = `${msg}: ${j.detail ?? detail}`
        } catch { msg = `${msg}: ${detail}` }
        throw new Error(msg)
      }
      const data: LogContent = await r.json()
      setPreviewContent(data)
    } catch (e) {
      // モーダルを閉じずエラーを内部に表示する。原因（404/CORS/ネットワーク等）を
      // ユーザーが直接読めるようにするため
      setPreviewError(e instanceof Error ? e.message : String(e))
    } finally {
      setPreviewLoading(false)
    }
  }

  const closePreview = () => {
    setPreviewing(null)
    setPreviewContent(null)
    setPreviewError(null)
  }

  const handleDelete = async (name: string) => {
    if (!confirm(`ログ "${name}" を削除しますか？\n（この操作は取り消せません）`)) return
    clearMessages()
    setDeletingName(name)
    try {
      const r = await apiFetch(`${API_BASE}/api/logs/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      })
      if (!r.ok) {
        const detail = await r.text()
        let msg = `HTTP ${r.status}`
        try {
          const j = JSON.parse(detail)
          msg = j.detail ?? msg
        } catch { /* not JSON */ }
        throw new Error(msg)
      }
      setInfo(`削除しました: ${name}`)
      await onLogsChange()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeletingName(null)
    }
  }

  return (
    <section className="log-manager">
      <div className="log-upload">
        <h3>新規アップロード</h3>
        <div className="log-upload-row">
          <input
            ref={fileInputRef}
            type="file"
            accept=".log"
            disabled={uploading}
            onChange={e => {
              const f = e.target.files?.[0]
              if (f) handleUpload(f)
            }}
          />
          <span className="log-upload-hint">
            .log のみ / 最大 10 MB / 同名ファイルは上書きされません
          </span>
        </div>
        {uploading && <div className="log-upload-status">アップロード中…</div>}
      </div>

      {error && (
        <div className="error">
          <strong>エラー:</strong> {error}
        </div>
      )}
      {info && (
        <div className="info">{info}</div>
      )}

      <div className="log-list-header">
        <h3>ログ一覧（{logs.length} 件）</h3>
        <button onClick={onLogsChange}>
          ⟳ 再読み込み
        </button>
      </div>

      {logs.length === 0 ? (
        <div className="log-empty">samples/logs/ にログがありません。アップロードしてください。</div>
      ) : (
        <div className="table-wrap">
          <table className="log-table">
            <thead>
              <tr>
                <th>ファイル名</th>
                <th>行数</th>
                <th>サイズ</th>
                <th>更新日時</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {logs.map(l => (
                <tr key={l.name}>
                  <td className="log-name"><code>{l.name}</code></td>
                  <td className="numeric">{l.lines.toLocaleString()}</td>
                  <td className="numeric">{formatBytes(l.bytes)}</td>
                  <td className="date">{formatDate(l.modified_at)}</td>
                  <td className="log-actions">
                    <button onClick={() => handlePreview(l.name)} className="btn-small">
                      プレビュー
                    </button>
                    <button
                      onClick={() => handleDelete(l.name)}
                      disabled={deletingName === l.name}
                      className="btn-small btn-delete"
                    >
                      {deletingName === l.name ? '削除中…' : '削除'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {previewing && (
        <div className="preview-modal-backdrop" onClick={closePreview}>
          <div className="preview-modal" onClick={e => e.stopPropagation()}>
            <div className="preview-modal-header">
              <h3>プレビュー: <code>{previewing}</code></h3>
              <button onClick={closePreview} className="btn-close" aria-label="閉じる">×</button>
            </div>
            <div className="preview-modal-body">
              {previewLoading && <div>読み込み中…</div>}
              {previewError && (
                <div className="error">
                  <strong>取得失敗:</strong> {previewError}
                  <div style={{ marginTop: '0.5rem', fontSize: '0.78rem' }}>
                    バックエンド再起動を忘れていませんか？ 新しいエンドポイントは uvicorn の再起動で反映されます。
                  </div>
                </div>
              )}
              {previewContent && (
                <>
                  <div className="preview-meta">
                    全 {previewContent.total_lines.toLocaleString()} 行のうち先頭 {previewContent.preview_lines} 行を表示
                    {previewContent.truncated && '（省略あり）'}
                    {' / '}
                    {formatBytes(previewContent.bytes)}
                  </div>
                  <pre className="preview-content">{previewContent.content}</pre>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
