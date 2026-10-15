/**
 * クラウド / SLM 精度比較タブ。
 *
 * 同じ入力（モデル以外の条件が同じ実行）を行に、モデル構成（cloud / local:<タグ> / mixed）を列にして、
 * 解析履歴の各実行を並べる。1 つの入力を構成ごとに複数回流す前提なので、各セルは実行のリスト。
 * 精度の主判定は手採点（Excel）なので、CSV を書き出して貼れるようにする。
 *
 * 設計: docs/plan/slm_support.md §9-6
 */
import { useCallback, useEffect, useState } from 'react'
import { API_BASE, apiFetch } from './api'
import { downloadText } from './reasoningReport'
import type { ComparisonResponse, ComparisonRun, FormatIssues } from './types'

interface Props {
  // 実行をクリックしたら解析履歴の詳細を開く
  onOpenRun: (historyId: number) => void
}

function profileLabel(profile: string): string {
  if (profile === 'cloud') return 'クラウド'
  if (profile === 'mixed') return '混在'
  return profile.startsWith('local:') ? `SLM ${profile.slice('local:'.length)}` : profile
}

function average(values: (number | null)[]): string {
  const xs = values.filter((v): v is number => v != null)
  if (xs.length === 0) return '-'
  return (xs.reduce((a, b) => a + b, 0) / xs.length).toFixed(2)
}

const ISSUE_LABELS: [keyof FormatIssues, string][] = [
  ['format_retries', '再生成'],
  ['parse_fallbacks', 'フォールバック'],
  ['routing_violations', '遷移違反'],
  ['input_truncated', '入力切り捨て'],
]

function IssueBadges({ issues }: { issues: FormatIssues }) {
  const shown = ISSUE_LABELS.filter(([k]) => issues[k] > 0)
  if (shown.length === 0) return null
  return (
    <div className="small" style={{ color: 'var(--warn, #b45309)' }}>
      {shown.map(([k, label]) => `${label} ${issues[k]}`).join(' / ')}
    </div>
  )
}

function RunItem({ run, index, onOpen }: { run: ComparisonRun; index: number; onOpen: () => void }) {
  return (
    <li style={{ marginBottom: 6 }}>
      <button type="button" className="link-button" onClick={onOpen} title={`解析履歴 #${run.id} を開く`}>
        {index + 1} 回目
      </button>{' '}
      採点 {run.eval_score ?? '未採点'} / 確信度 {run.confidence != null ? run.confidence.toFixed(2) : '-'}
      <div className="small muted">
        {run.top_category ? `[${run.top_category}] ` : ''}
        {(run.top_summary ?? '').slice(0, 80)}
        {(run.top_summary ?? '').length > 80 ? '…' : ''}
      </div>
      <IssueBadges issues={run.format_issues} />
    </li>
  )
}

export function ModelComparisonView({ onOpenRun }: Props) {
  const [data, setData] = useState<ComparisonResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await apiFetch(`${API_BASE}/api/analysis-history/comparison`)
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      setData(await r.json() as ComparisonResponse)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const downloadCsv = async () => {
    setError(null)
    try {
      const r = await apiFetch(`${API_BASE}/api/analysis-history/comparison.csv`)
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      // サーバーの BOM は text() で外れるので、downloadText が付け直す（Excel の文字化け防止）
      downloadText('slm_comparison.csv', await r.text(), 'text/csv;charset=utf-8;')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const profiles = data?.profiles ?? []

  return (
    <section className="run-history">
      <div className="run-history-header">
        <h2>クラウド / SLM 精度比較</h2>
        <div>
          <button type="button" className="btn-secondary" onClick={() => void load()} disabled={loading}>
            {loading ? '読み込み中…' : '再読み込み'}
          </button>{' '}
          <button type="button" className="btn-secondary" onClick={() => void downloadCsv()}>
            CSV をダウンロード（手採点用）
          </button>
        </div>
      </div>
      <p className="small muted">
        同じ入力（モデル以外の条件が同じ実行）を行、モデル構成を列に並べています。結果は実行ごとに揺れるため、
        構成ごとに複数回流して比べてください。精度の主判定は手採点です（CSV の manual_score 列に記入）。
        採点はモデル構成によらず Opus で行います。
      </p>
      {error && <div className="error">{error}</div>}
      {data && data.rows.length === 0 && (
        <div className="log-empty">
          比較できる履歴がありません。構成・ログ解析を実行するか、scripts/compare_slm.py で一括実行してください。
        </div>
      )}
      {data && data.rows.length > 0 && (
        <div className="table-wrap">
          <table className="run-history-table">
            <thead>
              <tr>
                <th>入力</th>
                {profiles.map(p => <th key={p}>{profileLabel(p)}</th>)}
              </tr>
            </thead>
            <tbody>
              {data.rows.map(row => (
                <tr key={row.input_fingerprint}>
                  <td style={{ verticalAlign: 'top' }}>
                    <div>{row.input_files.join(' / ') || '(ファイル名なし)'}</div>
                    <div className="small muted">指紋 {row.input_fingerprint}</div>
                  </td>
                  {profiles.map(p => {
                    const runs = row.cells[p] ?? []
                    return (
                      <td key={p} style={{ verticalAlign: 'top' }}>
                        {runs.length === 0 ? (
                          <span className="muted">未実行</span>
                        ) : (
                          <>
                            <div className="small">
                              {runs.length} 回 / 採点の平均 {average(runs.map(r => r.eval_score))} /
                              確信度の平均 {average(runs.map(r => r.confidence))}
                            </div>
                            <ol style={{ paddingLeft: 0, listStyle: 'none', margin: '4px 0 0' }}>
                              {runs.map((run, i) => (
                                <RunItem key={run.id} run={run} index={i} onOpen={() => onOpenRun(run.id)} />
                              ))}
                            </ol>
                          </>
                        )}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
