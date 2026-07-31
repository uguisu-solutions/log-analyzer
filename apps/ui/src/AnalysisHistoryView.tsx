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
import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import { ChatHistoryView } from './ChatHistoryView'
import { CombinedResultView, ResultTabs, StageResultView } from './ConfigLogAnalysis'
import { DelegationHistoryView } from './DelegationHistoryView'
import { EvaluationPanel } from './EvaluationPanel'
import { RoundMetricsView } from './RoundMetricsView'
import { ViewModeToggle } from './ViewModeToggle'
import { buildReasoningReport, downloadText } from './reasoningReport'
import { downloadTopologyDiagram } from './topologyImage'
import type {
  AnalysisHistoryDetail,
  AnalysisHistoryListResponse,
  AnalysisHistorySummary,
  ReanalyzeSeed,
  StageOutput,
  SuspectedNodeFinding,
  TopologyDef,
} from './types'

import { API_BASE, apiFetch } from './api'

interface Props {
  langfuseHost: string | null
  // 再解析 (docs/plan/reanalysis.md): 前回推論を種に config-log 画面へ引き継ぐ
  onReanalyze: (seed: ReanalyzeSeed) => void
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

export function AnalysisHistoryView({ langfuseHost, onReanalyze }: Props) {
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
      const r = await apiFetch(`${API_BASE}/api/analysis-history?${params}`)
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
      const r = await apiFetch(`${API_BASE}/api/analysis-history/${id}`)
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
      const r = await apiFetch(`${API_BASE}/api/analysis-history/${id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      setInfo(`解析履歴 #${id} を削除しました`)
      if (detail?.id === id) setDetail(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const hasFilters = useMemo(() => filterMode || searchQ, [filterMode, searchQ])

  // ─── 再解析の系譜でグループ化 (docs/plan/reanalysis.md) ───────
  // 同じ調査 (root_run_id) の版を revision 降順にまとめ、最新 N と 1 つ前 (N-1) を既定表示、
  // 2 回以上前 (revision <= N-2) はトグルで開閉する。
  const [expandedRoots, setExpandedRoots] = useState<Set<string>>(new Set())
  const groups = useMemo(() => {
    const map = new Map<string, AnalysisHistorySummary[]>()
    for (const e of entries) {
      const root = e.root_run_id || e.run_id
      const arr = map.get(root) ?? []
      arr.push(e)
      map.set(root, arr)
    }
    const gs = [...map.entries()].map(([root, rows]) => {
      const sorted = [...rows].sort(
        (a, b) => (b.revision - a.revision) || (a.created_at < b.created_at ? 1 : -1),
      )
      const maxRev = sorted.reduce((m, r) => Math.max(m, r.revision ?? 0), 0)
      return { root, rows: sorted, maxRev, latest: sorted[0] }
    })
    // グループは最新版の解析日時が新しい順
    gs.sort((a, b) => (a.latest.created_at < b.latest.created_at ? 1 : -1))
    return gs
  }, [entries])
  const toggleRoot = (root: string) => {
    setExpandedRoots(prev => {
      const next = new Set(prev)
      if (next.has(root)) next.delete(root)
      else next.add(root)
      return next
    })
  }

  // ─── 詳細ビュー (解析後画面の再現) ───────────────────────────
  if (detail) {
    return (
      <AnalysisHistoryDetailView
        detail={detail}
        langfuseHost={langfuseHost}
        onBack={() => setDetail(null)}
        onDelete={() => handleDelete(detail.id)}
        onReanalyze={onReanalyze}
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
              {groups.map(g => {
                const expanded = expandedRoots.has(g.root)
                // 既定表示 = 最新 (maxRev) と 1 つ前 (maxRev-1)。それ未満はトグルで開閉。
                const isDefaultVisible = (r: AnalysisHistorySummary) => (r.revision ?? 0) >= g.maxRev - 1
                const hiddenCount = g.rows.filter(r => !isDefaultVisible(r)).length
                const visibleRows = expanded ? g.rows : g.rows.filter(isDefaultVisible)
                const multiVersion = g.maxRev > 0
                return (
                  <Fragment key={g.root}>
                    {visibleRows.map(e => {
                      const isLatest = (e.revision ?? 0) === g.maxRev
                      return (
                        <tr key={e.id} onClick={() => openDetail(e.id)}
                          className={`clickable${isLatest ? '' : ' ah-old-version'}`}>
                          <td className="date">
                            {multiVersion && (
                              <span className={`ah-rev-badge${isLatest ? ' latest' : ''}`}>
                                v{e.revision ?? 0}{isLatest ? '（最新）' : ''}
                              </span>
                            )}
                            {formatDate(e.created_at)}
                          </td>
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
                      )
                    })}
                    {hiddenCount > 0 && (
                      <tr className="ah-toggle-row">
                        <td colSpan={8}>
                          <button className="link-button" onClick={() => toggleRoot(g.root)}>
                            {expanded
                              ? '▾ 過去の版を隠す'
                              : `▸ 過去の版 (${hiddenCount}件) を表示`}
                          </button>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
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
  onReanalyze: (seed: ReanalyzeSeed) => void
}

function AnalysisHistoryDetailView({ detail, langfuseHost, onBack, onDelete, onReanalyze }: DetailProps) {
  const topology: TopologyDef = detail.request?.topology ?? { image: null, imageWidth: 0, imageHeight: 0, nodes: [], links: [] }
  const result = detail.result
  const findings = result?.suspected_node_findings ?? []
  const qa = detail.request?.questionnaire_answers ?? {}
  const qconf = detail.request?.questionnaire_confidences ?? {}
  const traceUrl = langfuseHost && result?.trace_id ? `${langfuseHost}/trace/${result.trace_id}` : null

  // 表示モード (config-log 解析画面と同じ 標準/チャット 切替)。既定は「標準」。
  const [viewMode, setViewMode] = useState<'standard' | 'chat'>('standard')
  const [resultTab, setResultTab] = useState<'combined' | 'stage1' | 'stage2'>('combined')
  // 保存済み result から Stage 出力を復元 (標準モードの Stage タブ用)
  const isTwoStage = detail.analysis_mode === 'two_stage'
  const stageOutputs: StageOutput[] = result?.stage_outputs ?? []
  const stageOneOutput: StageOutput | null = stageOutputs[0] ?? null
  const stageTwoOutput: StageOutput | null = stageOutputs.length > 1 ? stageOutputs[1] : null

  return (
    <section className="topology-mode config-log-mode analysis-history-detail">
      <div className="ah-detail-bar">
        <button className="btn-secondary" onClick={onBack}>← 一覧へ戻る</button>
        <h2>解析履歴 #{detail.id}</h2>
        {result && (
          <button className="btn-secondary btn-small"
            onClick={() => {
              const base = result.trace_id?.slice(0, 8) || String(detail.id)
              downloadText(`reasoning-${base}.md`, buildReasoningReport(result))
              void downloadTopologyDiagram(`topology-${base}.png`, topology, result)
            }}>
            レポート＋構成図を出力
          </button>
        )}
        {result && (
          <button className="btn-small btn-reanalyze"
            title="この解析の推論を引き継ぎ、追加情報を足して再解析する"
            onClick={() => {
              onReanalyze({
                priorReasoning: buildReasoningReport(result),
                topology,
                configId: detail.config_id,
                parentRunId: detail.run_id,
                rootRunId: detail.root_run_id ?? detail.run_id,
                revision: (detail.revision ?? 0) + 1,
                prevFiles: detail.request?.input_files ?? [],
                prevRevision: detail.revision ?? 0,
                prevQuestionnaire: detail.request?.questionnaire_answers ?? {},
                prevConfidences: detail.request?.questionnaire_confidences ?? {},
                prevBigquery: detail.request?.node_bigquery ?? {},
              })
            }}>
            前回の推論をもとに再解析
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

      {/* 推論過程 + 最終結果の再現。config-log 解析画面と同じ 標準/チャット を切替できる（既定=標準）。 */}
      <h3>解析の経過と結果</h3>
      {result ? (
        <>
          <ViewModeToggle mode={viewMode} onChange={setViewMode} />
          {viewMode === 'chat' ? (
            <ChatHistoryView result={result} questionnaireAnswers={qa} questionnaireConfidences={qconf} />
          ) : (
            <section className="topology-result">
              <ResultTabs
                current={resultTab}
                onChange={setResultTab}
                isTwoStage={isTwoStage}
                stageOneOutput={stageOneOutput}
                stageTwoOutput={stageTwoOutput}
              />
              {resultTab === 'combined' && (
                <CombinedResultView result={result} isTwoStage={isTwoStage} stageOneOutput={stageOneOutput} stageTwoOutput={stageTwoOutput} topology={topology} langfuseHost={langfuseHost} />
              )}
              {resultTab === 'stage1' && stageOneOutput && (
                <StageResultView stage={stageOneOutput} topology={topology} />
              )}
              {resultTab === 'stage2' && stageTwoOutput && (
                <StageResultView stage={stageTwoOutput} topology={topology} />
              )}
            </section>
          )}
        </>
      ) : (
        <div className="log-empty">結果データがありません</div>
      )}

      {/* 監査所見は ChatHistoryView 内に会話として含まれる (重複回避のためここでは出さない) */}

      {/* 委譲チェーン (各監視の rationale / focus_hint。評価が参照する推論の跡) */}
      {result?.delegation_history && result.delegation_history.length > 0 && (
        <DelegationHistoryView result={result} />
      )}

      {/* ラウンド単位 metrics */}
      {result?.round_metrics && result.round_metrics.length > 0 && (
        <RoundMetricsView rounds={result.round_metrics} />
      )}

      {/* 解答と比較評価 (真因到達度の 10 段階採点、履歴に紐付け)。
          検証用の内部機能のため既定でマスク。検証時のみ VITE_SHOW_EVALUATION=1 で表示。 */}
      {import.meta.env.VITE_SHOW_EVALUATION === '1' && (
        <EvaluationPanel historyId={detail.id} />
      )}
    </section>
  )
}
