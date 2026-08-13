/**
 * 推定コストの表示ヘルパ (確認事項 D-2)。
 *
 * バックエンドが計算するのは **本解析のみ** の `metrics.cost_usd`
 * (orchestrator + 各監視 + integrator)。tokens と範囲を揃えるための判断で、
 * 方針プランナーと監査GPT の分はそれぞれ `policy_proposal` / `audit_report` に
 * ある model / tokens から**画面側で**算出し、別枠 + 合計として見せる。
 *
 * 単価はバックエンドの `tracing.py` の価格表と同じ値を持つ。UI 側は保存済みの
 * 履歴 (キャッシュ内訳を持たない古いデータ) でも概算を出せるようにするため、
 * キャッシュ倍率は考慮しない**上限値**として計算する。本解析の値は
 * バックエンドがキャッシュ込みで正確に計算したものをそのまま使う。
 */
import type { AnalysisResult } from './types'

// 1M トークンあたりの USD 単価 (input, output)。tracing.py と揃えること。
const PRICES: Record<string, [number, number]> = {
  'claude-fable-5': [10.0, 50.0],
  'claude-opus-4-8': [5.0, 25.0],
  'claude-opus-4-7': [5.0, 25.0],
  'claude-opus-4-6': [5.0, 25.0],
  'claude-opus-4-5': [5.0, 25.0],
  'claude-sonnet-4-6': [3.0, 15.0],
  'claude-sonnet-4-5': [3.0, 15.0],
  'claude-haiku-4-5': [1.0, 5.0],
  'gpt-5.5': [5.0, 30.0],
}

/** 単価表から概算コストを出す。未収載モデルは null。 */
export function estimateCost(
  model: string | undefined | null,
  tokensIn: number | undefined | null,
  tokensOut: number | undefined | null,
): number | null {
  if (!model) return null
  const key = model.trim().toLowerCase()
  const price = PRICES[key] ?? Object.entries(PRICES).find(([k]) => key.startsWith(k))?.[1]
  if (!price) return null
  return ((tokensIn ?? 0) / 1_000_000) * price[0] + ((tokensOut ?? 0) / 1_000_000) * price[1]
}

/** 表示用の整形。小さい額でも 0.00 にならないよう桁を調整する。 */
export function formatCost(usd: number | null | undefined): string {
  if (usd == null) return '—'
  if (usd === 0) return '$0'
  if (usd < 0.01) return `$${usd.toFixed(4)}`
  return `$${usd.toFixed(2)}`
}

export interface CostBreakdown {
  // 本解析 (バックエンドがキャッシュ込みで計算した値)。未計算なら null。
  main: number | null
  planner: number | null
  audit: number | null
  total: number | null
  // 単価未登録のモデルが混ざっている場合の注記 (info_loss_flags 由来)
  unpricedNote: string | null
}

/** 解析結果から「本解析 / プランナー / 監査 / 合計」の内訳を作る。 */
export function costBreakdown(result: AnalysisResult): CostBreakdown {
  // cost_usd が 0 の履歴は「対応前で未計算」なので null 扱いにする
  // (0 を「無料」と誤読させない)。
  const raw = result.metrics?.cost_usd
  const main = raw == null || raw === 0 ? null : raw
  const policy = result.policy_proposal
  const audit = result.audit_report
  const planner = policy ? estimateCost(policy.model, policy.tokens_in, policy.tokens_out) : null
  const auditCost = audit ? estimateCost(audit.model, audit.tokens_in, audit.tokens_out) : null
  const parts = [main, planner, auditCost].filter((v): v is number => v != null)
  const unpriced = (result.info_loss_flags ?? []).find(f => f.startsWith('cost_unpriced_models:'))
  return {
    main,
    planner,
    audit: auditCost,
    total: parts.length > 0 ? parts.reduce((a, b) => a + b, 0) : null,
    unpricedNote: unpriced ? unpriced.replace(/^cost_unpriced_models:\s*/, '') : null,
  }
}
