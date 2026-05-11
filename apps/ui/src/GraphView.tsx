import { useMemo } from 'react'
import ReactFlow, { Background, Controls, type Node, type Edge } from 'reactflow'
import 'reactflow/dist/style.css'

interface GraphNodeData {
  id: string
  label: string
  role: string
  model?: string | null
  latency_ms?: number | null
  tokens_in?: number | null
  tokens_out?: number | null
  metadata?: Record<string, unknown>
}

interface GraphEdgeData {
  source: string
  target: string
}

const ROLE_COLORS: Record<string, { bg: string; border: string }> = {
  filter: { bg: '#fef3c7', border: '#f59e0b' },
  triage: { bg: '#e0e7ff', border: '#6366f1' },
  analyze: { bg: '#d1fae5', border: '#10b981' },
  parallel_model: { bg: '#dbeafe', border: '#3b82f6' },
  integrator: { bg: '#fce7f3', border: '#db2777' },
  orchestrator: { bg: '#fef3c7', border: '#f59e0b' },
  monitor: { bg: '#dbeafe', border: '#3b82f6' },
  model_call: { bg: '#d1fae5', border: '#10b981' },
}

function nodeBody(n: GraphNodeData): string {
  const parts: string[] = []
  if (n.model) parts.push(n.model)
  if (n.latency_ms != null) parts.push(`${(n.latency_ms / 1000).toFixed(1)}s`)
  if (n.tokens_in != null && n.tokens_out != null) {
    parts.push(`tok ${n.tokens_in.toLocaleString()}/${n.tokens_out.toLocaleString()}`)
  }
  return parts.join(' · ')
}

interface Props {
  nodes: GraphNodeData[]
  edges: GraphEdgeData[]
}

export function GraphView({ nodes, edges }: Props) {
  const { rfNodes, rfEdges } = useMemo(() => layoutGraph(nodes, edges), [nodes, edges])

  if (nodes.length === 0) {
    return <div className="graph-empty">この構成は execution_graph を提供しません</div>
  }

  return (
    <div className="graph-container">
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        fitView
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        zoomOnScroll={false}
        panOnScroll={false}
      >
        <Background gap={16} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}

/** DFS でバックエッジを除いた DAG を返す（サイクル構造の longest-path 無限ループ回避）。 */
function dagEdges(nodes: GraphNodeData[], edges: GraphEdgeData[]): GraphEdgeData[] {
  const adj = new Map<string, string[]>()
  edges.forEach(e => {
    if (!adj.has(e.source)) adj.set(e.source, [])
    adj.get(e.source)!.push(e.target)
  })
  const visited = new Set<string>()
  const inStack = new Set<string>()
  const back = new Set<string>()
  function dfs(u: string) {
    visited.add(u)
    inStack.add(u)
    for (const v of adj.get(u) ?? []) {
      if (inStack.has(v)) back.add(`${u}->${v}`)
      else if (!visited.has(v)) dfs(v)
    }
    inStack.delete(u)
  }
  nodes.forEach(n => { if (!visited.has(n.id)) dfs(n.id) })
  return edges.filter(e => !back.has(`${e.source}->${e.target}`))
}

function layoutGraph(
  nodes: GraphNodeData[],
  edges: GraphEdgeData[],
): { rfNodes: Node[]; rfEdges: Edge[] } {
  // 深さ計算は back-edge を除いた DAG で行う（rfEdges 描画は元の cyclic edges のまま）
  const layoutEdges = dagEdges(nodes, edges)
  const incoming = new Map<string, number>()
  nodes.forEach(n => incoming.set(n.id, 0))
  layoutEdges.forEach(e => incoming.set(e.target, (incoming.get(e.target) ?? 0) + 1))

  const depth = new Map<string, number>()
  const queue: string[] = []
  nodes.forEach(n => {
    if ((incoming.get(n.id) ?? 0) === 0) {
      depth.set(n.id, 0)
      queue.push(n.id)
    }
  })
  let safety = nodes.length * nodes.length + 16
  while (queue.length > 0 && safety-- > 0) {
    const cur = queue.shift() as string
    const curDepth = depth.get(cur) ?? 0
    layoutEdges
      .filter(e => e.source === cur)
      .forEach(e => {
        const newDepth = curDepth + 1
        if ((depth.get(e.target) ?? -1) < newDepth) {
          depth.set(e.target, newDepth)
          queue.push(e.target)
        }
      })
  }

  // 深度ごとのバケット（順序保持）
  const byDepth = new Map<number, string[]>()
  nodes.forEach(n => {
    const d = depth.get(n.id) ?? 0
    if (!byDepth.has(d)) byDepth.set(d, [])
    byDepth.get(d)?.push(n.id)
  })

  const X_STEP = 240
  const Y_STEP = 110

  const rfNodes: Node[] = nodes.map(n => {
    const d = depth.get(n.id) ?? 0
    const peers = byDepth.get(d) ?? [n.id]
    const idx = peers.indexOf(n.id)
    const offset = (peers.length - 1) / 2
    const colors = ROLE_COLORS[n.role] ?? { bg: '#f3f4f6', border: '#9ca3af' }
    return {
      id: n.id,
      data: {
        label: (
          <div style={{ textAlign: 'left' }}>
            <div style={{ fontWeight: 700, fontSize: 12 }}>{n.id}</div>
            <div style={{ fontSize: 10, color: '#374151', marginTop: 2 }}>{n.role}</div>
            <div style={{ fontSize: 10, color: '#6b7280', marginTop: 2 }}>{nodeBody(n)}</div>
          </div>
        ),
      },
      position: { x: d * X_STEP, y: (idx - offset) * Y_STEP },
      style: {
        background: colors.bg,
        border: `2px solid ${colors.border}`,
        borderRadius: 6,
        padding: 8,
        minWidth: 180,
        boxShadow: '0 1px 3px rgba(0,0,0,0.06)',
      },
    }
  })

  const rfEdges: Edge[] = edges.map((e, i) => ({
    id: `e${i}`,
    source: e.source,
    target: e.target,
    animated: false,
    style: { stroke: '#94a3b8', strokeWidth: 1.5 },
  }))

  return { rfNodes, rfEdges }
}
