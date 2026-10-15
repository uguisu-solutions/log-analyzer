/**
 * 構成・ログ解析の「推論モデル」選択欄。
 *
 * クラウド（既定）か SLM（Ollama 経由の Gemma 4）を選び、構成4 の全ノードをそのモデルで動かす。
 * SLM の接続先はバックエンドの OLLAMA_BASE_URL で決まる（手元の Ollama か Runpod の SSH トンネル）。
 * 接続先に無いモデルは選べないようにする（実行してから「モデルが無い」で失敗するのを防ぐ）。
 *
 * 設計: docs/plan/slm_support.md §1 / §9-1
 */
import { useCallback, useEffect, useState } from 'react'
import { API_BASE, apiFetch } from './api'
import type { InferenceModelsResponse } from './types'

const OLLAMA_PREFIX = 'ollama:'

interface Props {
  // '' = クラウド（既定）。SLM は 'ollama:<タグ>'
  value: string
  // overrides は構成4 の各ノードに選んだモデルを割り当てたもの。クラウドなら undefined（従来どおり送らない）
  onChange: (model: string, overrides: Record<string, string> | undefined) => void
  disabled?: boolean
}

function endpointLabel(url: string): string {
  if (url.includes(':11435')) return 'Runpod（SSH トンネル）'
  if (url.includes(':11434')) return '手元の Ollama'
  return url
}

export function InferenceModelSelector({ value, onChange, disabled }: Props) {
  const [info, setInfo] = useState<InferenceModelsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await apiFetch(`${API_BASE}/api/inference-models`)
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      setInfo(await r.json() as InferenceModelsResponse)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  // 接続先が変わって選択中の SLM が無くなったら、クラウドに戻す
  useEffect(() => {
    if (!info || !value) return
    const current = info.models.find(m => m.id === value)
    if (!current?.available) onChange('', undefined)
  }, [info, value, onChange])

  const select = (model: string) => {
    if (!model || !info) {
      onChange('', undefined)
      return
    }
    onChange(model, Object.fromEntries(info.slots.map(s => [s, model])))
  }

  const cloud = info?.models[0]
  const slms = (info?.models ?? []).filter(m => m.id.startsWith(OLLAMA_PREFIX))

  return (
    <div className="advanced-settings-body" style={{ marginBottom: 8 }}>
      <label>
        推論モデル:{' '}
        <select value={value} onChange={e => select(e.target.value)} disabled={disabled || !info}>
          <option value="">クラウド（{cloud?.id ?? '既定'}・既定）</option>
          {slms.map(m => (
            <option key={m.id} value={m.id} disabled={!m.available}>
              SLM {m.id.slice(OLLAMA_PREFIX.length)}{m.available ? '' : '（接続先に無い）'}
            </option>
          ))}
        </select>
      </label>{' '}
      <button type="button" className="btn-small" onClick={() => void load()} disabled={loading || disabled}>
        {loading ? '確認中…' : '接続先を再確認'}
      </button>
      {info && (
        <div className="small muted">
          SLM の接続先: {endpointLabel(info.ollama_base_url)}（{info.ollama_base_url}）
          {info.ollama_reachable ? '' : ` — つながりません: ${info.ollama_error ?? ''}`}
          {value && ` / 構成4 の全ノード（${info.slots.length} 個）をこのモデルで動かします`}
        </div>
      )}
      {error && <div className="error">推論モデルの一覧を取得できません: {error}</div>}
    </div>
  )
}
