/**
 * 解析対象ソースコード（コードベース）の選択・アップロード パネル（Phase 3）。
 *
 * - 一覧 (GET /api/source) からコードベースを 1 つ選ぶ（または「使用しない」）。
 * - 複数ファイル（zip / 単体ソース混在）をまとめてアップロード (POST /api/source)。
 * - 選択中コードベースは config-log 解析の source_codebase として送られ、監視ノードが
 *   source_search / source_read / db_schema ツールでオンデマンド参照する。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { SourceCodebaseEntry } from './types'

const API_BASE = 'http://localhost:8000'
// アップロード合計の目安（バックエンドの上限と一致）
const MAX_TOTAL_BYTES = 50 * 1024 * 1024

interface Props {
  selected: string
  onSelect: (name: string) => void
  disabled?: boolean
}

function langSummary(langs: Record<string, number>): string {
  const entries = Object.entries(langs)
  if (entries.length === 0) return '—'
  return entries.map(([k, v]) => `${k}:${v}`).join(', ')
}

export function SourceCodebasePanel({ selected, onSelect, disabled }: Props) {
  const [list, setList] = useState<SourceCodebaseEntry[]>([])
  const [name, setName] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/api/source`)
      if (!r.ok) return
      const data = (await r.json()) as { codebases: SourceCodebaseEntry[] }
      setList(data.codebases ?? [])
    } catch {
      /* 一覧取得失敗は致命的でない（未起動時など） */
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const onFilesPicked = (picked: FileList | null) => {
    const arr = picked ? Array.from(picked) : []
    setFiles(arr)
    // 名前未入力なら最初のファイル名から推定（zip 拡張子は落とす）
    if (!name && arr.length > 0) {
      const base = arr[0].name.replace(/\.(zip|tar|gz)$/i, '').replace(/[^A-Za-z0-9._-]/g, '_')
      setName(base || 'codebase')
    }
  }

  const upload = async () => {
    setError(null)
    if (!name.trim()) { setError('コードベース名を入力してください'); return }
    if (files.length === 0) { setError('ファイルを選択してください'); return }
    const total = files.reduce((s, f) => s + f.size, 0)
    if (total > MAX_TOTAL_BYTES) {
      setError(`合計サイズが上限 (${MAX_TOTAL_BYTES / (1024 * 1024)}MB) を超えています`)
      return
    }
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('name', name.trim())
      for (const f of files) fd.append('files', f)
      const r = await fetch(`${API_BASE}/api/source`, { method: 'POST', body: fd })
      if (!r.ok) {
        const text = await r.text()
        throw new Error(`HTTP ${r.status}: ${text}`)
      }
      const entry = (await r.json()) as SourceCodebaseEntry
      await refresh()
      onSelect(entry.name)        // アップロードしたものを自動選択
      setFiles([])
      setName('')
      if (fileInputRef.current) fileInputRef.current.value = ''
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUploading(false)
    }
  }

  const remove = async (target: string) => {
    if (!confirm(`コードベース「${target}」を削除しますか？`)) return
    try {
      await fetch(`${API_BASE}/api/source/${encodeURIComponent(target)}`, { method: 'DELETE' })
      if (selected === target) onSelect('')
      await refresh()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const current = list.find(c => c.name === selected)

  return (
    <details className="source-panel mermaid-panel" open={!!selected}>
      <summary>
        <span className="qp-title">ソースコード（オンデマンド解析）</span>
        <span className="qp-hint muted">
          クリックで開閉。コードベースを選ぶと監視ノードが障害に関係しそうな箇所だけを参照します（任意）
        </span>
      </summary>

      <div className="source-controls" style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        <label>
          使用するコードベース:&nbsp;
          <select
            value={selected}
            disabled={disabled}
            onChange={e => onSelect(e.target.value)}
          >
            <option value="">（使用しない）</option>
            {list.map(c => (
              <option key={c.name} value={c.name}>
                {c.name}（{c.file_count} files / {c.table_count} tables）
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn-secondary btn-small" disabled={disabled} onClick={() => void refresh()}>
          再読込
        </button>
        {selected && (
          <button type="button" className="btn-secondary btn-small" disabled={disabled} onClick={() => void remove(selected)}>
            選択中を削除
          </button>
        )}
      </div>

      {current && (
        <div className="muted small" style={{ marginTop: 4 }}>
          {current.file_count} ファイル · {current.symbol_count} シンボル · {current.table_count} テーブル · 言語 [{langSummary(current.languages)}]
        </div>
      )}

      <div className="source-upload" style={{ marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        <input
          type="text"
          placeholder="コードベース名 (英数 . _ -)"
          value={name}
          disabled={disabled || uploading}
          onChange={e => setName(e.target.value)}
          style={{ minWidth: 200 }}
        />
        <label className="btn-file btn-small">
          ファイル選択（zip / ソース、複数可）
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            disabled={disabled || uploading}
            onChange={e => onFilesPicked(e.target.files)}
          />
        </label>
        <span className="muted small">
          {files.length > 0
            ? `${files.length} ファイル選択 (${(files.reduce((s, f) => s + f.size, 0) / 1024).toFixed(0)} KB)`
            : `合計 ${MAX_TOTAL_BYTES / (1024 * 1024)}MB まで`}
        </span>
        <button type="button" className="btn-secondary btn-small" disabled={disabled || uploading || files.length === 0} onClick={() => void upload()}>
          {uploading ? 'アップロード中…' : 'アップロード'}
        </button>
      </div>

      {error && <div className="error small" style={{ marginTop: 6 }}>{error}</div>}
    </details>
  )
}
