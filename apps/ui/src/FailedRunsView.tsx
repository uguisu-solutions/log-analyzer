/**
 * 失敗・中断した実行の一覧 (確認事項 B-4)。
 *
 * 解析履歴 (analysis_history) は「解析が最後まで到達したとき」しか保存されない。
 * そのため失敗・中断・方針却下の実行は解析履歴に現れず、顧客から
 * 「Langfuse にあるのに解析履歴に無い実行がある」という指摘につながった。
 *
 * バックエンドが失敗も実行履歴 (run_history) に status 付きで残すようになったので、
 * 顧客が実際に見る解析履歴タブの中で「解析履歴に出てこない実行」を確認できるようにする。
 * 既定は折りたたみ (正常時にノイズにしない)。
 */
import { useCallback, useEffect, useState } from 'react'
import { API_BASE, apiFetch } from './api'
import type { RunHistoryEntry, RunHistoryListResponse } from './types'

const STATUS_LABEL: Record<string, string> = {
  error: 'エラー',
  aborted: '中断',
  rejected: '方針却下',
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString('ja-JP', { hour12: false })
  } catch {
    return iso
  }
}

export function FailedRunsView() {
  const [entries, setEntries] = useState<RunHistoryEntry[]>([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await apiFetch(`${API_BASE}/api/runs/history?status=failed&limit=50`)
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const data: RunHistoryListResponse = await r.json()
      setEntries(data.entries)
      setTotal(data.total)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (!loading && total === 0 && !error) return null

  return (
    <details className="failed-runs">
      <summary className="failed-runs-head">
        <strong>失敗・中断した実行（{loading ? '…' : `${total} 件`}）</strong>
        <span className="muted small">
          解析が最後まで到達しなかった実行。結果が無いため解析履歴には保存されません。
        </span>
      </summary>

      {error && <div className="error"><strong>エラー:</strong> {error}</div>}

      <div className="table-wrap">
        <table className="run-history-table">
          <thead>
            <tr>
              <th>実行日時</th>
              <th>結果</th>
              <th>段階</th>
              <th>エラー内容</th>
              <th>tokens</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(e => {
              const status = e.status || 'error'
              return (
                <tr key={e.id}>
                  <td className="date">{formatDate(e.started_at)}</td>
                  <td>
                    <span className={`run-status run-status-${status}`}>
                      {STATUS_LABEL[status] ?? status}
                    </span>
                  </td>
                  <td><code>{e.error_stage || '-'}</code></td>
                  <td className="failed-runs-message">{e.error_message || '-'}</td>
                  <td className="numeric">
                    {(e.tokens_in ?? 0).toLocaleString()} / {(e.tokens_out ?? 0).toLocaleString()}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="muted small failed-runs-note">
        トークンが 0 の行は LLM を呼ぶ前の失敗（入力チェックなど）、
        0 でない行は方針プランナーだけ動いて中止された実行です。
      </p>
    </details>
  )
}
