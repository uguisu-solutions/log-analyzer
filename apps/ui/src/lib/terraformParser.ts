/**
 * Terraform HCL の簡易パーサ (Phase E 拡張)。
 *
 * 議事録/ユーザー要求: 「terraform ファイルをアップロードして各ノードに一括で
 * 設定を割り当て」たい。バックエンド側に HCL ライブラリを導入せず、フロントで
 * 正規表現 + ブレース深さトラッキングで `resource "<type>" "<label>" { ... }`
 * ブロックを抽出する。
 *
 * 制約:
 * - resource のみサポート (data / module / variable / locals 等は無視)
 * - HCL の完全な文法解析ではない。文字列リテラル内の {} はスキップする
 *   程度の手堅さで、コメント // や /* *\/ は粗く対応
 * - 余裕で誤マッチが出る可能性はある (PoC 範囲では許容)
 */
import type { TopologyNode } from '../types'

export interface TfResource {
  type: string
  label: string
  /** name 属性や tags.Name から拾った識別子 (任意) */
  extractedName?: string
  /** `resource "..." "..." { ... }` を含む完全な HCL テキスト */
  fullBlock: string
}

export interface TfMatchResult {
  resource: TfResource
  matchedNodeId: string | null
  matchedBy: 'label' | 'name' | 'tag_name' | null
}

/**
 * ノードID と HCL ラベル / name を比較しやすくする。
 *
 * 例:
 *   "fw-01"     → "fw-01"
 *   "fw_01"     → "fw-01"
 *   "AWS_FW.01" → "aws-fw-01"
 */
export function normalizeName(s: string | undefined | null): string {
  if (!s) return ''
  return s.toLowerCase().replace(/[._]/g, '-').trim()
}

/**
 * HCL の文字列リテラルやコメントをスキップしながら、resource ブロック本文の
 * 終端位置 (対応する `}` のインデックス) を返す。
 *
 * @param src   元テキスト
 * @param from  最初の `{` の "次" の位置 (深さ 1 で始まる前提)
 * @returns 対応する `}` の位置 (= ブロックの末尾、その文字を含めない)。
 *          見つからなければ src.length を返す。
 */
function findBlockEnd(src: string, from: number): number {
  let depth = 1
  let i = from
  while (i < src.length && depth > 0) {
    const ch = src[i]
    // 行コメント //
    if (ch === '/' && src[i + 1] === '/') {
      const nl = src.indexOf('\n', i)
      i = nl === -1 ? src.length : nl + 1
      continue
    }
    // 行コメント #
    if (ch === '#') {
      const nl = src.indexOf('\n', i)
      i = nl === -1 ? src.length : nl + 1
      continue
    }
    // ブロックコメント /* */
    if (ch === '/' && src[i + 1] === '*') {
      const end = src.indexOf('*/', i + 2)
      i = end === -1 ? src.length : end + 2
      continue
    }
    // 文字列リテラル "..." (バックスラッシュエスケープを許容)
    if (ch === '"') {
      i++
      while (i < src.length && src[i] !== '"') {
        if (src[i] === '\\') i++
        i++
      }
      i++  // 終端の " をスキップ
      continue
    }
    // ヒアドキュメント <<EOF ... EOF はざっくり「次の行頭が EOF」で抜ける
    // 完全対応は重いので簡易判定
    if (ch === '<' && src[i + 1] === '<') {
      // 識別子の終端まで読む
      let j = i + 2
      if (src[j] === '-') j++  // <<- も許容
      const idStart = j
      while (j < src.length && /[A-Za-z0-9_]/.test(src[j])) j++
      const marker = src.slice(idStart, j)
      if (marker) {
        const closeRe = new RegExp(`^\\s*${marker}\\b`, 'm')
        const m = closeRe.exec(src.slice(j))
        if (m) {
          i = j + m.index + m[0].length
          continue
        }
      }
    }
    if (ch === '{') depth++
    else if (ch === '}') {
      depth--
      if (depth === 0) return i
    }
    i++
  }
  return Math.min(i, src.length)
}

/**
 * resource ブロック内から `name = "..."` または `tags { Name = "..." }` 風の
 * 識別子を雑に拾う。複雑なオブジェクト式や heredoc は無視。
 */
function extractName(body: string): string | undefined {
  // name = "..."
  const nameMatch = body.match(/(?:^|[\s,{])name\s*=\s*"([^"\n]+)"/m)
  if (nameMatch) return nameMatch[1]
  // tags = { Name = "..." } または tags { Name = "..." }
  const tagsBlockMatch = body.match(/tags\s*=?\s*\{([\s\S]*?)\}/)
  if (tagsBlockMatch) {
    const tn = tagsBlockMatch[1].match(/Name\s*=\s*"([^"\n]+)"/i)
    if (tn) return tn[1]
  }
  return undefined
}

/** HCL ソースから resource ブロックを抽出。 */
export function parseTerraform(src: string): TfResource[] {
  const out: TfResource[] = []
  // resource "type" "label" {
  const headerRe = /resource\s+"([^"\n]+)"\s+"([^"\n]+)"\s*\{/g
  let m: RegExpExecArray | null
  while ((m = headerRe.exec(src)) !== null) {
    const [header, type, label] = m
    const bodyStart = m.index + header.length
    const bodyEnd = findBlockEnd(src, bodyStart)
    const body = src.slice(bodyStart, bodyEnd)
    const extractedName = extractName(body)
    const fullBlock = src.slice(m.index, bodyEnd + 1)  // 終端 } を含む
    out.push({ type, label, extractedName, fullBlock })
    headerRe.lastIndex = bodyEnd + 1
  }
  return out
}

/** resources を nodes にマッチング (label / extractedName を node id と正規化比較)。 */
export function matchResourcesToNodes(
  resources: TfResource[],
  nodes: TopologyNode[],
): TfMatchResult[] {
  const nodeKeys = new Map<string, string>()
  for (const n of nodes) {
    const k = normalizeName(n.id)
    if (k) nodeKeys.set(k, n.id)
  }
  return resources.map(r => {
    const tryMatch = (candidate: string | undefined, by: TfMatchResult['matchedBy']):
      TfMatchResult | null => {
      const k = normalizeName(candidate)
      const nid = k ? nodeKeys.get(k) : undefined
      if (nid) return { resource: r, matchedNodeId: nid, matchedBy: by }
      return null
    }
    return (
      tryMatch(r.extractedName, 'name') ||
      tryMatch(r.label, 'label') ||
      { resource: r, matchedNodeId: null, matchedBy: null }
    )
  })
}
