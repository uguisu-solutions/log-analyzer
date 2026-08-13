/**
 * ラウンド単位集計の表示 (Phase D)。
 *
 * 議事録「ラウンド履歴、消費トークン、処理時間をラウンド単位で閲覧可能にする」
 * に対応。各ラウンドの role / model / tokens / latency をテーブル + 簡易バーで表示。
 * cost は LLM 単価をハードコードせず、tokens / latency のみ可視化する (Phase F の
 * 比較ベンチマークで計算したくなったら別途追加)。
 */
import { useMemo } from 'react'
import { estimateCost, formatCost } from './cost'
import type { PlannerUsage } from './PolicySummaryView'
import type { RoundMetrics } from './types'

interface Props {
  rounds: RoundMetrics[]
  // 方針プランナー (解析前に 1 回) の消費量。確認事項 A-2 で追加。
  // ラウンドではないため round 列は "—"、合計は「本解析」と「プランナー込み」を
  // 併記する (metrics.tokens_in/out にはプランナー分が入っていないため)。
  planner?: PlannerUsage | null
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

export function RoundMetricsView({ rounds, planner }: Props) {
  const { totals, maxTokens, maxLatency } = useMemo(() => {
    const t_in = rounds.reduce((s, r) => s + r.tokens_in, 0)
    const t_out = rounds.reduce((s, r) => s + r.tokens_out, 0)
    const lat = rounds.reduce((s, r) => s + r.latency_ms, 0)
    const costs = rounds.map(r => r.cost_usd).filter((v): v is number => v != null)
    // バーの尺度はプランナー行も含めて揃える (プランナーだけ突出/潰れないように)
    const tokenValues = rounds.map(r => r.tokens_in + r.tokens_out)
    const latencyValues = rounds.map(r => r.latency_ms)
    if (planner) {
      tokenValues.push(planner.tokensIn + planner.tokensOut)
      latencyValues.push(planner.latencyMs)
    }
    return {
      totals: {
        in: t_in, out: t_out, latency: lat,
        cost: costs.length > 0 ? costs.reduce((a, b) => a + b, 0) : null,
      },
      maxTokens: Math.max(1, ...tokenValues),
      maxLatency: Math.max(1, ...latencyValues),
    }
  }, [rounds, planner])

  if (rounds.length === 0) return null

  return (
    <section className="round-metrics">
      <div className="round-metrics-header">
        <h4>ラウンド別 リソース消費</h4>
        <span className="round-metrics-totals muted">
          計 tokens: {totals.in.toLocaleString()} in / {totals.out.toLocaleString()} out ·
          {' '}計 latency: {(totals.latency / 1000).toFixed(1)}s · {rounds.length} ステップ
          {totals.cost != null && <> · 計コスト: {formatCost(totals.cost)}</>}
          {planner && (
            <>
              {' '}／ プランナー込み: {(totals.in + planner.tokensIn).toLocaleString()} in /
              {' '}{(totals.out + planner.tokensOut).toLocaleString()} out ·
              {' '}{((totals.latency + planner.latencyMs) / 1000).toFixed(1)}s
            </>
          )}
        </span>
      </div>
      <table className="round-metrics-table">
        <thead>
          <tr>
            <th>round</th>
            <th>役割</th>
            <th>model</th>
            <th>tokens (in/out)</th>
            <th>latency (s)</th>
            <th>コスト</th>
          </tr>
        </thead>
        <tbody>
          {/* 方針プランナー: ラウンド 0 より前に 1 回だけ動くため先頭に置く。
              上の「計」はラウンドのみの合算なので、この行は合算対象外である旨を添える。 */}
          {planner && (
            <tr className="round-row role-planner">
              <td className="rm-round">—</td>
              <td className="rm-role">
                方針プランナー
                <span className="muted small rm-role-note">（計に含まず）</span>
              </td>
              <td className="rm-model">{planner.model || '-'}</td>
              <td className="rm-tokens">
                <div className="rm-bar-cell">
                  <div
                    className="rm-bar rm-bar-tokens"
                    style={{ width: `${((planner.tokensIn + planner.tokensOut) / maxTokens) * 100}%` }}
                  />
                  <span className="rm-bar-text">
                    {planner.tokensIn.toLocaleString()} / {planner.tokensOut.toLocaleString()}
                  </span>
                </div>
              </td>
              <td className="rm-latency">
                <div className="rm-bar-cell">
                  <div
                    className="rm-bar rm-bar-latency"
                    style={{ width: `${(planner.latencyMs / maxLatency) * 100}%` }}
                  />
                  <span className="rm-bar-text">{(planner.latencyMs / 1000).toFixed(2)}s</span>
                </div>
              </td>
              <td className="rm-cost numeric">
                {formatCost(estimateCost(planner.model, planner.tokensIn, planner.tokensOut))}
              </td>
            </tr>
          )}
          {rounds.map((r, i) => {
            const tok = r.tokens_in + r.tokens_out
            const tokenPct = (tok / maxTokens) * 100
            const latPct = (r.latency_ms / maxLatency) * 100
            return (
              <tr key={i} className={`round-row role-${r.role}`}>
                <td className="rm-round">r{r.round}</td>
                <td className="rm-role">{ROLE_LABEL[r.role] ?? r.role}</td>
                <td className="rm-model">{r.model || '-'}</td>
                <td className="rm-tokens">
                  <div className="rm-bar-cell">
                    <div className="rm-bar rm-bar-tokens" style={{ width: `${tokenPct}%` }} />
                    <span className="rm-bar-text">
                      {r.tokens_in.toLocaleString()} / {r.tokens_out.toLocaleString()}
                    </span>
                  </div>
                  {/* prompt caching の内訳 (確認事項 D-2/C-1)。キャッシュ読み出しは
                      1/10 単価なので、コストの内訳としてここに出す。 */}
                  {(r.cache_read ?? 0) + (r.cache_creation ?? 0) > 0 && (
                    <div className="rm-cache muted small">
                      cache 読出 {(r.cache_read ?? 0).toLocaleString()}
                      {(r.cache_creation ?? 0) > 0 && ` / 書込 ${(r.cache_creation ?? 0).toLocaleString()}`}
                    </div>
                  )}
                </td>
                <td className="rm-latency">
                  <div className="rm-bar-cell">
                    <div className="rm-bar rm-bar-latency" style={{ width: `${latPct}%` }} />
                    <span className="rm-bar-text">{(r.latency_ms / 1000).toFixed(2)}s</span>
                  </div>
                </td>
                <td className="rm-cost numeric">{formatCost(r.cost_usd)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </section>
  )
}
