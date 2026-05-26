/**
 * ラウンド単位集計の表示 (Phase D)。
 *
 * 議事録「ラウンド履歴、消費トークン、処理時間をラウンド単位で閲覧可能にする」
 * に対応。各ラウンドの role / model / tokens / latency をテーブル + 簡易バーで表示。
 * cost は LLM 単価をハードコードせず、tokens / latency のみ可視化する (Phase F の
 * 比較ベンチマークで計算したくなったら別途追加)。
 */
import { useMemo } from 'react'
import type { RoundMetrics } from './types'

interface Props {
  rounds: RoundMetrics[]
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

export function RoundMetricsView({ rounds }: Props) {
  const { totals, maxTokens, maxLatency } = useMemo(() => {
    const t_in = rounds.reduce((s, r) => s + r.tokens_in, 0)
    const t_out = rounds.reduce((s, r) => s + r.tokens_out, 0)
    const lat = rounds.reduce((s, r) => s + r.latency_ms, 0)
    return {
      totals: { in: t_in, out: t_out, latency: lat },
      maxTokens: Math.max(1, ...rounds.map(r => r.tokens_in + r.tokens_out)),
      maxLatency: Math.max(1, ...rounds.map(r => r.latency_ms)),
    }
  }, [rounds])

  if (rounds.length === 0) return null

  return (
    <section className="round-metrics">
      <div className="round-metrics-header">
        <h4>ラウンド別 リソース消費</h4>
        <span className="round-metrics-totals muted">
          計 tokens: {totals.in.toLocaleString()} in / {totals.out.toLocaleString()} out ·
          {' '}計 latency: {(totals.latency / 1000).toFixed(1)}s · {rounds.length} ステップ
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
          </tr>
        </thead>
        <tbody>
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
                </td>
                <td className="rm-latency">
                  <div className="rm-bar-cell">
                    <div className="rm-bar rm-bar-latency" style={{ width: `${latPct}%` }} />
                    <span className="rm-bar-text">{(r.latency_ms / 1000).toFixed(2)}s</span>
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </section>
  )
}
