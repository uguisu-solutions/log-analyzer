/**
 * 解析履歴タブ。
 *
 * config-log 解析の各実行を「入力 + Claude の推論過程 + 結果」付きで保存したものを
 * 一覧 → 詳細で表示し、**解析終了後の画面の状態を再現**する。
 *
 * - 一覧: サマリ (日時 / モード / 確信度 / tokens / top) のテーブル
 * - 詳細: 構成図キャンバス (画像 + ノード矩形 + severity ハイライト) +
 *         ChatHistoryView (会話形式の推論過程 + 最終結果) + ラウンド metrics
 *
 * 設計: docs/plan/analysis_history.md
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { ChatHistoryView } from './ChatHistoryView'
import { RoundMetricsView } from './RoundMetricsView'
import { buildReasoningCsv, downloadCsv } from './reasoningCsv'
import type {
  AnalysisHistoryDetail,
  AnalysisHistoryListResponse,
  AnalysisHistorySummary,
  SuspectedNodeFinding,
  TopologyDef,
} from './types'

const API_BASE = 'http://localhost:8000'

interface Props {
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

const SOURCE_LABEL: Record<string, string> = {
  config: 'config のみ',
  log: 'log のみ',
  both: 'config + log',
}
const ORDER_LABEL: Record<string, string> = {
  config_log: 'config → log',
  log_config: 'log → config',
}

function modeText(e: { analysis_mode: string | null; single_source: string | null; stage_order: string | null }): string {
  if (e.analysis_mode === 'single') {
    return `1 段階 (${SOURCE_LABEL[e.single_source ?? ''] ?? e.single_source ?? '?'})`
  }
  if (e.analysis_mode === 'two_stage') {
    return `2 段階 (${ORDER_LABEL[e.stage_order ?? ''] ?? e.stage_order ?? '?'})`
  }
  return e.analysis_mode ?? '-'
}

export function AnalysisHistoryView({ langfuseHost }: Props) {
  const [entries, setEntries] = useState<AnalysisHistorySummary[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)

  // フィルタ
  const [filterMode, setFilterMode] = useState<string>('')
  const [searchQ, setSearchQ] = useState<string>('')
  const [debouncedQ, setDebouncedQ] = useState<string>('')
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(searchQ), 300)
    return () => clearTimeout(t)
  }, [searchQ])

  // 詳細表示中のエントリ
  const [detail, setDetail] = useState<AnalysisHistoryDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      if (filterMode) params.set('analysis_mode', filterMode)
      if (debouncedQ) params.set('q', debouncedQ)
      params.set('limit', '200')
      const r = await fetch(`${API_BASE}/api/analysis-history?${params}`)
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const data: AnalysisHistoryListResponse = await r.json()
      setEntries(data.entries)
      setTotal(data.total)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [filterMode, debouncedQ])

  useEffect(() => { load() }, [load])

  const openDetail = async (id: number) => {
    setDetailLoading(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/api/analysis-history/${id}`)
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const data: AnalysisHistoryDetail = await r.json()
      setDetail(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDetailLoading(false)
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm(`解析履歴 #${id} を削除しますか？`)) return
    setError(null); setInfo(null)
    try {
      const r = await fetch(`${API_BASE}/api/analysis-history/${id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      setInfo(`解析履歴 #${id} を削除しました`)
      if (detail?.id === id) setDetail(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const hasFilters = useMemo(() => filterMode || searchQ, [filterMode, searchQ])

  // ─── 詳細ビュー (解析後画面の再現) ───────────────────────────
  if (detail) {
    return (
      <AnalysisHistoryDetailView
        detail={detail}
        langfuseHost={langfuseHost}
        onBack={() => setDetail(null)}
        onDelete={() => handleDelete(detail.id)}
      />
    )
  }

  // ─── 一覧ビュー ──────────────────────────────────────────────
  return (
    <section className="run-history">
      <section className="run-history-filters">
        <div className="run-history-filters-title">フィルタ</div>
        <div className="run-history-filters-row">
          <label>
            <span className="filter-label">モード</span>
            <select value={filterMode} onChange={e => setFilterMode(e.target.value)}>
              <option value="">（全て）</option>
              <option value="single">1 段階</option>
              <option value="two_stage">2 段階</option>
            </select>
          </label>
          <label className="flex-grow">
            <span className="filter-label">テキスト検索</span>
            <input
              type="text"
              value={searchQ}
              onChange={e => setSearchQ(e.target.value)}
              placeholder="top 要約 / 見出しに部分一致"
            />
          </label>
          <button className="btn-secondary" onClick={() => { setFilterMode(''); setSearchQ('') }} disabled={!hasFilters}>
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
        <h3>解析履歴（{loading ? '...' : `${entries.length} / ${total} 件`}）</h3>
        {detailLoading && <span className="muted">詳細を読み込み中…</span>}
      </div>

      {entries.length === 0 && !loading ? (
        <div className="log-empty">
          {hasFilters
            ? 'フィルタ条件に一致する解析履歴がありません'
            : 'まだ解析履歴がありません。「config-log 解析」タブで解析を実行すると、完了時に自動保存されます。'}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="run-history-table">
            <thead>
              <tr>
                <th>解析日時</th>
                <th>モード</th>
                <th>確信度</th>
                <th>tokens</th>
                <th>レイテンシ</th>
                <th>top</th>
                <th>要約</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(e => (
                <tr key={e.id} onClick={() => openDetail(e.id)} className="clickable">
                  <td className="date">{formatDate(e.created_at)}</td>
                  <td className="ah-mode-cell">{modeText(e)}</td>
                  <td className="numeric">{e.confidence?.toFixed(2) ?? '-'}</td>
                  <td className="numeric">
                    {(e.tokens_in ?? 0).toLocaleString()} / {(e.tokens_out ?? 0).toLocaleString()}
                  </td>
                  <td className="numeric">{formatLatency(e.latency_ms)}</td>
                  <td>{e.top_category && <span className={`badge cat-${e.top_category}`}>{e.top_category}</span>}</td>
                  <td className="ah-summary-cell">{e.top_summary ?? '-'}</td>
                  <td className="ah-actions" onClick={ev => ev.stopPropagation()}>
                    <div className="ah-actions-inner">
                      <button onClick={() => openDetail(e.id)} className="btn-small">詳細</button>
                      <button onClick={() => handleDelete(e.id)} className="btn-small btn-delete">削除</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// ─── severity ハイライト (ConfigLogAnalysis と同等ロジック) ──────
function highlightClass(findings: SuspectedNodeFinding[], nodeId: string): string {
  const f = findings.find(x => x.node_id === nodeId)
  if (!f) return ''
  const sev = f.severity || ''
  if (sev === 'info') return ''
  if (sev === 'secondary') return 'is-suspected sev-secondary'
  return 'is-suspected sev-primary'
}

interface DetailProps {
  detail: AnalysisHistoryDetail
  langfuseHost: string | null
  onBack: () => void
  onDelete: () => void
}

function AnalysisHistoryDetailView({ detail, langfuseHost, onBack, onDelete }: DetailProps) {
  const topology: TopologyDef = detail.request?.topology ?? { image: null, imageWidth: 0, imageHeight: 0, nodes: [], links: [] }
  const result = detail.result
  const findings = result?.suspected_node_findings ?? []
  const qa = detail.request?.questionnaire_answers ?? {}
  const traceUrl = langfuseHost && result?.trace_id ? `${langfuseHost}/trace/${result.trace_id}` : null

  return (
    <section className="topology-mode config-log-mode analysis-history-detail">
      <div className="ah-detail-bar">
        <button className="btn-secondary" onClick={onBack}>← 一覧へ戻る</button>
        <h2>解析履歴 #{detail.id}</h2>
        {result && (
          <button className="btn-secondary btn-small"
            onClick={() => downloadCsv(`reasoning-${result.trace_id?.slice(0, 8) || detail.id}.csv`, buildReasoningCsv(result))}>
            推論過程を CSV 出力
          </button>
        )}
        <button className="btn-small btn-delete" onClick={onDelete}>削除</button>
      </div>

      <dl className="run-detail ah-detail-meta">
        <dt>解析日時</dt><dd>{formatDate(detail.created_at)}</dd>
        <dt>モード</dt><dd>{modeText(detail)}</dd>
        <dt>確信度</dt><dd>{result?.confidence?.toFixed(3) ?? '-'}</dd>
        <dt>tokens (in / out)</dt><dd>{(result?.metrics?.tokens_in ?? 0).toLocaleString()} / {(result?.metrics?.tokens_out ?? 0).toLocaleString()}</dd>
        <dt>レイテンシ</dt><dd>{formatLatency(result?.metrics?.latency_ms_total ?? null)}</dd>
        <dt>trace_id</dt><dd className="mono small">
          {traceUrl ? <a href={traceUrl} target="_blank" rel="noopener noreferrer" className="trace-link">{result.trace_id} ↗</a> : (result?.trace_id ?? '-')}
        </dd>
      </dl>

      {/* 構成図キャンバスの再現 (読み取り専用) */}
      <h3>構成図（障害候補ハイライト）</h3>
      {topology.image ? (
        <div className="topology-canvas ah-readonly-canvas">
          <img src={topology.image} alt="topology" className="topology-image" draggable={false} />
          <svg className="topology-overlay" viewBox="0 0 1 1" preserveAspectRatio="none">
            {topology.nodes.map(n => {
              const hl = highlightClass(findings, n.id)
              return (
                <g key={n.id}>
                  <rect x={n.x} y={n.y} width={n.w} height={n.h}
                    className={['node-rect', hl].filter(Boolean).join(' ')} vectorEffect="non-scaling-stroke" />
                  <text x={n.x + 0.005} y={n.y + 0.018} className={['node-label', hl].filter(Boolean).join(' ')}>
                    {n.id}{n.type ? ` [${n.type}]` : ''}
                  </text>
                </g>
              )
            })}
          </svg>
        </div>
      ) : (
        <div className="topology-empty">（構成図画像は保存されていません）</div>
      )}

      {/* 推論過程 + 最終結果 (会話形式) の再現 */}
      <h3>解析の経過と結果</h3>
      {result ? (
        <ChatHistoryView result={result} questionnaireAnswers={qa} />
      ) : (
        <div className="log-empty">結果データがありません</div>
      )}

      {/* 監査所見は ChatHistoryView 内に会話として含まれる (重複回避のためここでは出さない) */}

      {/* ラウンド単位 metrics */}
      {result?.round_metrics && result.round_metrics.length > 0 && (
        <RoundMetricsView rounds={result.round_metrics} />
      )}
    </section>
  )
}
