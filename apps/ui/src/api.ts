/**
 * API 呼び出しの共通ヘルパー。
 *
 * - `API_BASE`: バックエンドの URL。環境変数 `VITE_API_BASE` があればそれを使い、
 *   無ければ従来どおり `http://localhost:8000`（ローカル開発は無設定で現状維持）。
 * - `apiFetch`: `fetch` の薄いラッパ。`VITE_API_KEY` が設定されているときだけ
 *   `X-API-Key` ヘッダを付与する（未設定なら付けない＝ローカルは無害）。
 *
 * ホスティング時のみ Vercel 側で `VITE_API_BASE` / `VITE_API_KEY` を注入する。
 */

// 末尾スラッシュを正規化（`${API_BASE}/api/...` の二重スラッシュを防ぐ）
export const API_BASE = (import.meta.env.VITE_API_BASE ?? 'http://localhost:8000').replace(/\/+$/, '')

const API_KEY = (import.meta.env.VITE_API_KEY ?? '').trim()

/**
 * `fetch` 互換のラッパ。
 * - `input` が絶対 URL（http で始まる）ならそのまま、相対パスなら `API_BASE` を前置。
 * - `VITE_API_KEY` があれば `X-API-Key` を付与。
 */
export function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const url = input.startsWith('http') ? input : `${API_BASE}${input}`
  const headers = new Headers(init.headers)
  if (API_KEY) headers.set('X-API-Key', API_KEY)
  return fetch(url, { ...init, headers })
}
