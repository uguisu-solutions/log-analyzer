/**
 * 解答と比較評価パネル (評価機能 Phase 3)。
 *
 * 解析履歴の詳細で、模範解答 (Excel「テストケース2」由来のシナリオ) を選び、
 * LLM (既定 Opus4.7) にレポートの推論支援価値を 10 段階採点させる。良い点/悪い点/
 * ⑦罠の回避を表示し、過去の評価も履歴に紐づけて後から確認できる。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { downloadText } from './reasoningReport'
import type { AnswerScenario, EvaluationDTO } from './types'

const API_BASE = 'http://localhost:8000'

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString('ja-JP', { hour12: false })
  } catch {
    return iso
  }
}

/** 1 評価を md レポートに整形する (個別出力用)。使用トークンも含める。 */
function buildEvaluationReport(ev: EvaluationDTO, scenarioTitle: string): string {
  const L: string[] = []
  L.push('# 解析評価レポート（解答との比較）')
  L.push('')
  L.push('## 概要')
  L.push(`- 解答シナリオ: ${ev.scenario_key}${scenarioTitle ? ` — ${scenarioTitle}` : ''}`)
  L.push(`- スコア: **${ev.score == null ? '-' : ev.score} / 10**`)
  L.push(`- 判定モデル: ${ev.model || '-'}`)
  L.push(`- 使用トークン (in / out): ${(ev.tokens_in ?? 0).toLocaleString()} / ${(ev.tokens_out ?? 0).toLocaleString()}`)
  if (ev.latency_ms != null) L.push(`- レイテンシ: ${(ev.latency_ms / 1000).toFixed(1)}s`)
  L.push(`- 評価日時: ${formatDate(ev.created_at)}`)
  L.push(`- 解析履歴 ID: #${ev.analysis_history_id}`)
  L.push('')
  L.push('## 配点内訳（推論支援価値）')
  L.push(`- スコア: **${ev.score == null ? '-' : ev.score} / 10** — ${scoreBand(ev.score)}`)
  L.push(`- ⑦ジュニアの落とし穴（参考）: 回避を助けた ${ev.pitfalls_avoided.length}件 / 踏んだ ${ev.pitfalls_hit.length}件`)
  L.push('')
  if (ev.axis_assessment.length > 0) {
    L.push('## 採点根拠（観点別）')
    for (const a of ev.axis_assessment) L.push(`- ${a}`)
    L.push('')
  }
  if (ev.summary) {
    L.push('## 総評')
    L.push(ev.summary)
    L.push('')
  }
  const sec = (title: string, items: string[], prefix = '') => {
    if (items.length === 0) return
    L.push(`## ${title}`)
    for (const it of items) L.push(`- ${prefix}${it}`)
    L.push('')
  }
  sec('良い点', ev.good_points)
  sec('悪い点', ev.bad_points)
  sec('避けた罠 (⑦)', ev.pitfalls_avoided, '✓ ')
  sec('踏んだ罠 (⑦)', ev.pitfalls_hit, '✗ ')
  return L.join('\n')
}

function scoreClass(score: number | null): string {
  const s = score ?? 0
  if (s >= 7) return 'eval-score-high'
  if (s >= 4) return 'eval-score-mid'
  return 'eval-score-low'
}

/** スコア → 推論支援価値のルーブリック帯ラベル (配点内訳の説明用)。 */
function scoreBand(score: number | null): string {
  const s = score ?? -1
  if (s >= 9) return '複数パス・可能性を根拠付きで提示、未確認/前提ズレも明示'
  if (s >= 7) return '主要な推論の道筋と除外理由を提示、不足も一部明示'
  if (s >= 5) return '一定の推論はあるが単一結論寄り／視野拡張が弱い'
  if (s >= 3) return '断定的で別可能性・根拠が乏しい'
  if (s >= 1) return '単一の結論のみ。別案・不足・根拠の明示なし'
  return '評価失敗/未採点'
}

function EvaluationCard(
  { ev, scenarioTitle, onDelete }:
  { ev: EvaluationDTO; scenarioTitle: string; onDelete: (id: number) => void },
) {
  const exportReport = () => {
    const fname = `evaluation-${ev.scenario_key}-h${ev.analysis_history_id}-${ev.id}.md`
    downloadText(fname, buildEvaluationReport(ev, scenarioTitle))
  }
  return (
    <div className="eval-card">
      <div className="eval-card-head">
        <span className={`eval-score ${scoreClass(ev.score)}`}>
          {ev.score == null ? '-' : ev.score}
          <span className="eval-score-max">/10</span>
        </span>
        <span className="eval-scenario">解答: <strong>{ev.scenario_key}</strong></span>
        <span className="eval-meta muted">
          {ev.model}
          {` · ${(ev.tokens_in ?? 0).toLocaleString()}/${(ev.tokens_out ?? 0).toLocaleString()} tok`}
          {ev.latency_ms != null && ` · ${(ev.latency_ms / 1000).toFixed(1)}s`}
          {' · '}{formatDate(ev.created_at)}
        </span>
        <span className="eval-card-actions">
          <button className="btn-small" onClick={exportReport}>レポート出力</button>
          <button className="btn-small btn-delete" onClick={() => onDelete(ev.id)}>削除</button>
        </span>
      </div>

      <div className="eval-breakdown">
        <div className="eval-breakdown-title">配点内訳（推論支援価値）</div>
        <ul>
          <li>
            スコア: <strong>{ev.score == null ? '-' : ev.score}/10</strong>
            {' — '}{scoreBand(ev.score)}
          </li>
          <li>
            ⑦ジュニアの落とし穴 <span className="muted">（参考）</span>
            {` — 回避を助けた ${ev.pitfalls_avoided.length} / 踏んだ ${ev.pitfalls_hit.length}`}
          </li>
        </ul>
      </div>

      {ev.axis_assessment.length > 0 && (
        <div className="eval-axes">
          <div className="eval-sec-title">採点根拠（観点別）</div>
          <ul className="eval-list">{ev.axis_assessment.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      )}

      {ev.summary && <p className="eval-summary">{ev.summary}</p>}

      <div className="eval-cols">
        {ev.good_points.length > 0 && (
          <div className="eval-col">
            <div className="eval-sec-title good">良い点 ({ev.good_points.length})</div>
            <ul className="eval-list good">{ev.good_points.map((g, i) => <li key={i}>{g}</li>)}</ul>
          </div>
        )}
        {ev.bad_points.length > 0 && (
          <div className="eval-col">
            <div className="eval-sec-title bad">悪い点 ({ev.bad_points.length})</div>
            <ul className="eval-list bad">{ev.bad_points.map((b, i) => <li key={i}>{b}</li>)}</ul>
          </div>
        )}
      </div>

      {(ev.pitfalls_avoided.length > 0 || ev.pitfalls_hit.length > 0) && (
        <div className="eval-cols">
          {ev.pitfalls_avoided.length > 0 && (
            <div className="eval-col">
              <div className="eval-sec-title good">避けた罠 ⑦ ({ev.pitfalls_avoided.length})</div>
              <ul className="eval-list good">{ev.pitfalls_avoided.map((p, i) => <li key={i}>✓ {p}</li>)}</ul>
            </div>
          )}
          {ev.pitfalls_hit.length > 0 && (
            <div className="eval-col">
              <div className="eval-sec-title bad">踏んだ罠 ⑦ ({ev.pitfalls_hit.length})</div>
              <ul className="eval-list bad">{ev.pitfalls_hit.map((p, i) => <li key={i}>✗ {p}</li>)}</ul>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function EvaluationPanel({ historyId }: { historyId: number }) {
  const [scenarios, setScenarios] = useState<AnswerScenario[]>([])
  const [selected, setSelected] = useState('')
  const [evals, setEvals] = useState<EvaluationDTO[]>([])
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const titleByKey = useMemo(
    () => Object.fromEntries(scenarios.map(s => [s.scenario_key, s.title])),
    [scenarios],
  )

  useEffect(() => {
    let alive = true
    fetch(`${API_BASE}/api/answer-scenarios`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { if (alive) setScenarios(d.scenarios ?? []) })
      .catch(e => { if (alive) setError(e instanceof Error ? e.message : String(e)) })
    return () => { alive = false }
  }, [])

  const loadEvals = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/api/analysis-history/${historyId}/evaluations`)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const d = await r.json()
      // リスト項目を必ず配列に正規化（古い評価やバックエンド版差で欠けても
      // 描画時に undefined.length で全画面クラッシュしないようにする防御）。
      const norm: EvaluationDTO[] = (d.evaluations ?? []).map((e: EvaluationDTO) => ({
        ...e,
        axis_assessment: e.axis_assessment ?? [],
        good_points: e.good_points ?? [],
        bad_points: e.bad_points ?? [],
        pitfalls_avoided: e.pitfalls_avoided ?? [],
        pitfalls_hit: e.pitfalls_hit ?? [],
      }))
      setEvals(norm)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [historyId])

  useEffect(() => { loadEvals() }, [loadEvals])

  const runEval = async () => {
    if (!selected) return
    setRunning(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/api/analysis-history/${historyId}/evaluate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scenario_key: selected }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      await loadEvals()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  const del = async (evalId: number) => {
    if (!confirm(`評価 #${evalId} を削除しますか？`)) return
    try {
      const r = await fetch(`${API_BASE}/api/analysis-history/${historyId}/evaluations/${evalId}`, {
        method: 'DELETE',
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      await loadEvals()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <section className="eval-panel">
      <h3>解答と比較評価</h3>
      <p className="muted eval-help">
        模範解答 (テストケース2) を選び、このレポートが<strong>ジュニアの思考を正しい方向へ
        広げられたか（推論支援価値）</strong>を LLM が 10 段階で採点します（真因への一致そのものではなく、
        推論の道筋・視野の拡張・不足の明示・前提ズレの指摘を評価）。評価は履歴に残り、後から確認できます。
      </p>
      <div className="eval-controls">
        <select value={selected} onChange={e => setSelected(e.target.value)}>
          <option value="">解答シナリオを選択…</option>
          {scenarios.map(s => (
            <option key={s.scenario_key} value={s.scenario_key}>
              {s.scenario_key}: {s.title}
            </option>
          ))}
        </select>
        <button onClick={runEval} disabled={!selected || running}>
          {running ? '評価中…（Opus）' : '評価する'}
        </button>
      </div>

      {error && <div className="error"><strong>エラー:</strong> {error}</div>}

      {evals.length === 0 ? (
        <div className="muted eval-empty">
          まだ評価がありません。解答シナリオを選んで「評価する」を押してください。
        </div>
      ) : (
        <div className="eval-cards">
          {evals.map(ev => (
            <EvaluationCard
              key={ev.id}
              ev={ev}
              scenarioTitle={titleByKey[ev.scenario_key] ?? ''}
              onDelete={del}
            />
          ))}
        </div>
      )}
    </section>
  )
}
