import { useMemo } from 'react'
import ReactFlow, { Background, Controls, type Node, type Edge } from 'reactflow'
import 'reactflow/dist/style.css'
import { layoutWithDagre } from './dagreLayout'

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
  kind?: string | null
  label?: string | null
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

function layoutGraph(
  nodes: GraphNodeData[],
  edges: GraphEdgeData[],
): { rfNodes: Node[]; rfEdges: Edge[] } {
  // dagre で階層レイアウト。feedback エッジは描画専用なのでレイアウト対象から除外
  const forwardEdges = edges.filter(e => e.kind !== 'feedback')
  const positions = layoutWithDagre(nodes, forwardEdges, {
    nodeWidth: 200,
    nodeHeight: 80,
    rankdir: 'LR',
    nodesep: 50,
    ranksep: 90,
  })

  const rfNodes: Node[] = nodes.map(n => {
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
      position: positions[n.id] ?? { x: 0, y: 0 },
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

  const rfEdges: Edge[] = edges.map((e, i) => {
    const isFeedback = e.kind === 'feedback'
    return {
      id: `e${i}`,
      source: e.source,
      target: e.target,
      type: 'smoothstep',
      animated: isFeedback,
      label: e.label ?? undefined,
      labelStyle: isFeedback ? { fill: '#b45309', fontSize: 10, fontWeight: 700 } : undefined,
      labelBgStyle: isFeedback ? { fill: '#fef3c7', fillOpacity: 0.9 } : undefined,
      labelBgPadding: isFeedback ? ([4, 2] as [number, number]) : undefined,
      labelBgBorderRadius: isFeedback ? 3 : undefined,
      style: isFeedback
        ? { stroke: '#f59e0b', strokeWidth: 1.5, strokeDasharray: '6 4' }
        : { stroke: '#94a3b8', strokeWidth: 1.5 },
    }
  })

  return { rfNodes, rfEdges }
}
