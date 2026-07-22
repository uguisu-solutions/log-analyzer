import { next } from '@vercel/edge'

// Vercel Edge Middleware: フロント（静的SPA）全体に Basic 認証をかける。
// 認証情報は Vercel の環境変数 BASIC_AUTH_USER / BASIC_AUTH_PASS で管理する。
// 未設定のときは締め出しを防ぐため素通しする（デプロイ設定漏れでのロック回避）。
export const config = {
  // すべてのパスに適用（HTML・JS・アセットとも初回に Authorization を要求）。
  matcher: '/(.*)',
}

export default function middleware(request: Request) {
  const user = process.env.BASIC_AUTH_USER || ''
  const pass = process.env.BASIC_AUTH_PASS || ''

  // 資格情報が未設定なら認証をかけない（フェイルオープン＝ロックアウト防止）。
  if (!user || !pass) return next()

  const header = request.headers.get('authorization') || ''
  if (header.startsWith('Basic ')) {
    try {
      const decoded = atob(header.slice(6))
      const idx = decoded.indexOf(':')
      const u = decoded.slice(0, idx)
      const p = decoded.slice(idx + 1)
      if (u === user && p === pass) return next()
    } catch {
      // デコード失敗時は下の 401 へフォールスルー
    }
  }

  return new Response('Authentication required.', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="log-analyzer", charset="UTF-8"',
    },
  })
}
