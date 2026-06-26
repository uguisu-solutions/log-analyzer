/**
 * 構成図 (画像 + ノード矩形 + 障害候補レイヤーの色付け) を PNG 画像として出力する。
 *
 * 画面の topology-overlay (App.css) と同じ配色:
 *   - primary (直接原因)        … 赤   stroke #b3261e / fill rgba(179,38,30,0.30)
 *   - secondary (影響を受けた側) … 橙   stroke #b8860b / fill rgba(184,134,11,0.22)
 *   - info / 非該当             … 通常 stroke #0a5fb7 / fill rgba(10,95,183,0.10)
 * severity 判定は ConfigLogAnalysis.highlightClass と一致 (suspected かつ info は通常表示)。
 *
 * 単一実行・連続実行・履歴詳細のいずれからも同じロジックでダウンロードできる。
 */
import type { AnalysisResult, TopologyDef } from './types'

type NodeColor = { stroke: string; fill: string; label: string; bold: boolean }

const DEFAULT_COLOR: NodeColor = { stroke: '#0a5fb7', fill: 'rgba(10,95,183,0.10)', label: '#1a1a1a', bold: false }
const PRIMARY_COLOR: NodeColor = { stroke: '#b3261e', fill: 'rgba(179,38,30,0.30)', label: '#7a0e08', bold: true }
const SECONDARY_COLOR: NodeColor = { stroke: '#b8860b', fill: 'rgba(184,134,11,0.22)', label: '#6b4e08', bold: true }

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('topology image load failed'))
    img.src = src
  })
}

function colorFor(suspected: Set<string>, sevById: Map<string, string>, nodeId: string): NodeColor {
  if (!suspected.has(nodeId)) return DEFAULT_COLOR
  const sev = sevById.get(nodeId) || ''
  if (sev === 'info') return DEFAULT_COLOR  // info はハイライトしない (画面と一致)
  if (sev === 'secondary') return SECONDARY_COLOR
  return PRIMARY_COLOR  // primary / '' / その他
}

/** 構成図を PNG の dataURL にレンダリングする。画像が無ければ null。 */
export async function renderTopologyDiagramPng(
  topology: TopologyDef | null | undefined,
  result: AnalysisResult | null | undefined,
): Promise<string | null> {
  if (!topology?.image) return null
  const img = await loadImage(topology.image)
  // SVG など naturalSize が 0 の場合は保存サイズ → 既定にフォールバック
  const baseW = img.naturalWidth || topology.imageWidth || 1600
  const baseH = img.naturalHeight || topology.imageHeight || 900
  const maxW = 2000
  const scale = baseW > maxW ? maxW / baseW : 1
  const W = Math.max(1, Math.round(baseW * scale))
  const H = Math.max(1, Math.round(baseH * scale))

  const canvas = document.createElement('canvas')
  canvas.width = W
  canvas.height = H
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  ctx.drawImage(img, 0, 0, W, H)

  const suspected = new Set(result?.suspected_node_ids ?? [])
  const sevById = new Map<string, string>(
    (result?.suspected_node_findings ?? []).map(f => [f.node_id, f.severity || '']),
  )
  const fontPx = Math.max(11, Math.round(H * 0.018))

  for (const n of topology.nodes ?? []) {
    const c = colorFor(suspected, sevById, n.id)
    const x = n.x * W, y = n.y * H, w = n.w * W, h = n.h * H
    ctx.fillStyle = c.fill
    ctx.fillRect(x, y, w, h)
    ctx.lineWidth = 2.5
    ctx.strokeStyle = c.stroke
    ctx.strokeRect(x, y, w, h)

    const text = `${n.id}${n.type ? ` [${n.type}]` : ''}`
    ctx.font = `${c.bold ? 'bold ' : ''}${fontPx}px ui-monospace, "SF Mono", monospace`
    ctx.textBaseline = 'top'
    const tx = x + 4, ty = y + 3
    // 白縁取りで可読性確保 (画面の paint-order: stroke 相当)
    ctx.lineWidth = 3
    ctx.strokeStyle = '#ffffff'
    ctx.strokeText(text, tx, ty)
    ctx.fillStyle = c.label
    ctx.fillText(text, tx, ty)
  }
  return canvas.toDataURL('image/png')
}

/** PNG dataURL をファイルとしてダウンロードする。 */
function downloadDataUrl(filename: string, dataUrl: string): void {
  const a = document.createElement('a')
  a.href = dataUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
}

/** 構成図 (色付き) を PNG でダウンロードする。画像が無ければ false。 */
export async function downloadTopologyDiagram(
  filename: string,
  topology: TopologyDef | null | undefined,
  result: AnalysisResult | null | undefined,
): Promise<boolean> {
  try {
    const url = await renderTopologyDiagramPng(topology, result)
    if (!url) return false
    downloadDataUrl(filename, url)
    return true
  } catch (e) {
    console.warn('構成図のダウンロードに失敗しました:', e)
    return false
  }
}
