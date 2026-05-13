import { useCallback, useEffect, useMemo, useState } from 'react'
import type {
  ConfigEntry,
  LogEntry,
  RunHistoryEntry,
  RunHistoryListResponse,
} from './types'

const API_BASE = 'http://localhost:8000'

interface Props {
  configList: ConfigEntry[]
  logs: LogEntry[]
  langfuseHost: string | null
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString('ja-JP', { hour12: false })
  } catch {
    return iso
  }
}

function formatLatency(ms: number | null): string {
  if (ms == null) return '-'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

export function RunHistoryView({ configList, logs, langfuseHost }: Props) {
  const [entries, setEntries] = useState<RunHistoryEntry[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const [selected, setSelected] = useState<RunHistoryEntry | null>(null)

  // フィルタ
  const [filterLog, setFilterLog] = useState<string>('')
  const [filterConfig, setFilterConfig] = useState<string>('')
  const [searchQ, setSearchQ] = useState<string>('')
  // クライアント側で type=text の入力をデバウンスして送る
  const [debouncedQ, setDebouncedQ] = useState<string>('')
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(searchQ), 300)
    return () => clearTimeout(t)
  }, [searchQ])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      if (filterLog) params.set('log_name', filterLog)
      if (filterConfig) params.set('config_id', filterConfig)
      if (debouncedQ) params.set('q', debouncedQ)
      params.set('limit', '200')
      const r = await fetch(`${API_BASE}/api/runs/history?${params}`)
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const data: RunHistoryListResponse = await r.json()
      setEntries(data.entries)
      setTotal(data.total)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [filterLog, filterConfig, debouncedQ])

  useEffect(() => {
    load()
  }, [load])

  const handleDelete = async (id: number) => {
    if (!confirm(`実行履歴 #${id} を削除しますか？`)) return
    setError(null)
    setInfo(null)
    try {
      const r = await fetch(`${API_BASE}/api/runs/history/${id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      setInfo(`実行履歴 #${id} を削除しました`)
      // 開いている詳細が削除対象なら閉じる
      if (selected?.id === id) setSelected(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const handleResetFilters = () => {
    setFilterLog('')
    setFilterConfig('')
    setSearchQ('')
  }

  // 設定中のフィルタが有効か（クリアボタン活性化用）
  const hasFilters = useMemo(
    () => filterLog || filterConfig || searchQ,
    [filterLog, filterConfig, searchQ],
  )

  return (
    <section className="run-history">
      <section className="run-history-filters">
        <div className="run-history-filters-title">フィルタ</div>
        <div className="run-history-filters-row">
          <label>
            <span className="filter-label">ログ</span>
            <select value={filterLog} onChange={e => setFilterLog(e.target.value)}>
              <option value="">（全て）</option>
              {logs.map(l => (
                <option key={l.name} value={l.name}>{l.name}</option>
              ))}
            </select>
          </label>
          <label>
            <span className="filter-label">構成</span>
            <select value={filterConfig} onChange={e => setFilterConfig(e.target.value)}>
              <option value="">（全て）</option>
              {configList.map(c => (
                <option key={c.id} value={c.id}>{c.label}</option>
              ))}
            </select>
          </label>
          <label className="flex-grow">
            <span className="filter-label">テキスト検索</span>
            <input
              type="text"
              value={searchQ}
              onChange={e => setSearchQ(e.target.value)}
              placeholder="ログ名や top_summary に部分一致"
            />
          </label>
          <button
            className="btn-secondary"
            onClick={handleResetFilters}
            disabled={!hasFilters}
          >
            クリア
          </button>
          <button onClick={load} disabled={loading}>
            {loading ? '読み込み中…' : '⟳ 再読み込み'}
          </button>
        </div>
      </section>

      {error && <div className="error"><strong>エラー:</strong> {error}</div>}
      {info && <div className="info">{info}</div>}

      <div className="run-history-header">
        <h3>
          実行履歴（{loading ? '...' : `${entries.length} / ${total} 件`}
          {entries.length < total && '、上位 200 件まで'}）
        </h3>
        <button onClick={load} disabled={loading}>
          {loading ? '読み込み中…' : '⟳ 再読み込み'}
        </button>
      </div>

      {entries.length === 0 && !loading ? (
        <div className="log-empty">
          {hasFilters ? 'フィルタ条件に一致する履歴がありません' : 'まだ実行履歴がありません。単一実行 / 構成比較タブから実行してください'}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="run-history-table">
            <thead>
              <tr>
                <th>実行日時</th>
                <th>ログ</th>
                <th>構成</th>
                <th>確信度</th>
                <th>tokens</th>
                <th>レイテンシ</th>
                <th>top</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(e => (
                <tr
                  key={e.id}
                  onClick={() => setSelected(e)}
                  className={selected?.id === e.id ? 'selected' : ''}
                >
                  <td className="date">{formatDate(e.started_at)}</td>
                  <td><code>{e.log_name}</code></td>
                  <td>
                    <span className={`pill cfg-${e.base_config}`}>{e.config_id}</span>
                  </td>
                  <td className="numeric">{e.confidence?.toFixed(2) ?? '-'}</td>
                  <td className="numeric">
                    {(e.tokens_in ?? 0).toLocaleString()} / {(e.tokens_out ?? 0).toLocaleString()}
                  </td>
                  <td className="numeric">{formatLatency(e.latency_ms)}</td>
                  <td>
                    {e.top_category && (
                      <span className={`badge cat-${e.top_category}`}>{e.top_category}</span>
                    )}
                  </td>
                  <td className="run-history-actions" onClick={ev => ev.stopPropagation()}>
                    <button onClick={() => setSelected(e)} className="btn-small">詳細</button>
                    <button
                      onClick={() => handleDelete(e.id)}
                      className="btn-small btn-delete"
                    >
                      削除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <div className="preview-modal-backdrop" onClick={() => setSelected(null)}>
          <div className="preview-modal" onClick={ev => ev.stopPropagation()}>
            <div className="preview-modal-header">
              <h3>実行履歴 #{selected.id}</h3>
              <button onClick={() => setSelected(null)} className="btn-close" aria-label="閉じる">×</button>
            </div>
            <div className="preview-modal-body">
              <dl className="run-detail">
                <dt>実行日時</dt><dd>{formatDate(selected.started_at)}</dd>
                <dt>ログ</dt><dd><code>{selected.log_name}</code></dd>
                <dt>構成</dt><dd>
                  <span className={`pill cfg-${selected.base_config}`}>{selected.config_id}</span>
                  <span className="text-muted">（base: {selected.base_config}）</span>
                </dd>
                <dt>確信度</dt><dd>{selected.confidence?.toFixed(3) ?? '-'}</dd>
                <dt>tokens (in / out)</dt><dd>
                  {(selected.tokens_in ?? 0).toLocaleString()} / {(selected.tokens_out ?? 0).toLocaleString()}
                </dd>
                <dt>レイテンシ</dt><dd>{formatLatency(selected.latency_ms)}</dd>
                <dt>top カテゴリ</dt><dd>
                  {selected.top_category ? (
                    <span className={`badge cat-${selected.top_category}`}>{selected.top_category}</span>
                  ) : '-'}
                </dd>
                <dt>top 要約</dt><dd>{selected.top_summary ?? '-'}</dd>
                <dt>trace_id</dt><dd className="mono small">
                  {langfuseHost && selected.trace_id ? (
                    <a
                      href={`${langfuseHost}/trace/${selected.trace_id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="trace-link"
                    >
                      {selected.trace_id} ↗
                    </a>
                  ) : selected.trace_id ?? '-'}
                </dd>
              </dl>
              <p className="text-muted run-detail-hint">
                詳細なエージェント実行ログ・LLM 入出力は Langfuse の trace_id リンクから確認できます。
              </p>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
