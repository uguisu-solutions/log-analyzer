import { useEffect, useMemo, useState } from 'react'
import ReactFlow, {
  Background,
  Controls,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  type Edge,
  type Node,
  type NodeMouseHandler,
} from 'reactflow'
import 'reactflow/dist/style.css'
import type {
  BuiltinStructureNode,
  BuiltinStructureResponse,
  SlotInfo,
} from './types'

const API_BASE = 'http://localhost:8000'

interface RFData {
  kind: BuiltinStructureNode['type']
  slot_id?: string
  fixed_model?: string
  label: string
  modified: boolean
}

interface Props {
  baseConfig: string  // "config1".."config4"
  slots: SlotInfo[]
  promptOverrides: Record<string, string>
  modelOverrides: Record<string, string>
  onPromptChange: (slotId: string, value: string) => void
  onModelChange: (slotId: string, value: string) => void
  disabled?: boolean
}

const KIND_STYLES: Record<BuiltinStructureNode['type'], { bg: string; border: string }> = {
  input: { bg: '#fef3c7', border: '#f59e0b' },
  static: { bg: '#fef3c7', border: '#d97706' },
  slot: { bg: '#dbeafe', border: '#3b82f6' },
  slot_instance: { bg: '#e0e7ff', border: '#6366f1' },
}

function autoLayout(
  nodes: BuiltinStructureNode[],
  edges: { source: string; target: string }[],
): Record<string, { x: number; y: number }> {
  const indeg: Record<string, number> = {}
  nodes.forEach(n => (indeg[n.id] = 0))
  edges.forEach(e => (indeg[e.target] = (indeg[e.target] ?? 0) + 1))
  const depth: Record<string, number> = {}
  const queue: string[] = []
  nodes.forEach(n => {
    if ((indeg[n.id] ?? 0) === 0) {
      depth[n.id] = 0
      queue.push(n.id)
    }
  })
  while (queue.length > 0) {
    const cur = queue.shift() as string
    const cd = depth[cur] ?? 0
    edges
      .filter(e => e.source === cur)
      .forEach(e => {
        if ((depth[e.target] ?? -1) < cd + 1) {
          depth[e.target] = cd + 1
          queue.push(e.target)
        }
      })
  }
  const byDepth: Record<number, string[]> = {}
  nodes.forEach(n => {
    const d = depth[n.id] ?? 0
    ;(byDepth[d] ??= []).push(n.id)
  })
  const X_STEP = 240
  const Y_STEP = 130
  const out: Record<string, { x: number; y: number }> = {}
  Object.entries(byDepth).forEach(([dStr, ids]) => {
    const d = Number(dStr)
    const offset = (ids.length - 1) / 2
    ids.forEach((id, idx) => {
      out[id] = { x: 30 + d * X_STEP, y: 60 + (idx - offset) * Y_STEP + 200 }
    })
  })
  return out
}

function CanvasInner({
  baseConfig,
  slots,
  promptOverrides,
  modelOverrides,
  onPromptChange,
  onModelChange,
  disabled,
}: Props) {
  const [structure, setStructure] = useState<BuiltinStructureResponse | null>(null)
  const [nodes, setNodes, onNodesChange] = useNodesState<RFData>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // base が変わったら構造取得
  useEffect(() => {
    setSelectedNodeId(null)
    setError(null)
    fetch(`${API_BASE}/api/configs/${baseConfig}/structure`)
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: BuiltinStructureResponse) => setStructure(d))
      .catch(e => setError(`構造取得失敗: ${e.message}`))
  }, [baseConfig])

  // 修正済み slot の集合（バッジ表示用）
  const modifiedSlotIds = useMemo(() => {
    const out = new Set<string>()
    for (const slot of slots) {
      const p = promptOverrides[slot.slot_id]
      const m = modelOverrides[slot.slot_id]
      if ((p != null && p !== slot.default_prompt) || (m != null && m !== slot.default_model)) {
        out.add(slot.slot_id)
      }
    }
    return out
  }, [slots, promptOverrides, modelOverrides])

  // structure / modified の変化で React Flow ノードを再構築
  useEffect(() => {
    if (!structure) return
    const positions = autoLayout(structure.nodes, structure.edges)
    const rfNodes: Node<RFData>[] = structure.nodes.map(n => {
      const style = KIND_STYLES[n.type]
      const modified = !!(n.slot_id && modifiedSlotIds.has(n.slot_id))
      // ノードラベル: 1 段目=構造ラベル + 変更マーク、2 段目=slot/model 情報
      const subline = n.fixed_model
        ? `model: ${n.fixed_model}（固定）`
        : n.slot_id
        ? `slot: ${n.slot_id}`
        : ''
      const modBadge = modified ? ' ✏︎' : ''
      const label = subline ? `${n.label}${modBadge}\n${subline}` : `${n.label}${modBadge}`
      return {
        id: n.id,
        position: positions[n.id] ?? { x: 50, y: 100 },
        data: {
          kind: n.type,
          slot_id: n.slot_id,
          fixed_model: n.fixed_model,
          label,
          modified,
        },
        style: {
          background: style.bg,
          border: `${modified ? '3px' : '2px'} solid ${modified ? '#dc2626' : style.border}`,
          borderRadius: 6,
          padding: '8px 12px',
          minWidth: 180,
          fontSize: 12,
          whiteSpace: 'pre-line',
          textAlign: 'center',
        },
        deletable: false,
        connectable: false,
      } as Node<RFData>
    })

    const rfEdges: Edge[] = structure.edges.map((e, i) => ({
      id: `e_${e.source}_${e.target}_${i}`,
      source: e.source,
      target: e.target,
      style: { stroke: '#94a3b8', strokeWidth: 1.5 },
    }))

    setNodes(rfNodes)
    setEdges(rfEdges)
  }, [structure, modifiedSlotIds, setNodes, setEdges])

  const onNodeClick: NodeMouseHandler = (_, node) => {
    setSelectedNodeId(node.id)
  }

  const selectedStructureNode =
    structure && selectedNodeId
      ? structure.nodes.find(n => n.id === selectedNodeId)
      : null
  const selectedSlot =
    selectedStructureNode?.slot_id
      ? slots.find(s => s.slot_id === selectedStructureNode.slot_id)
      : null

  return (
    <div className="builtin-canvas-wrap">
      <div className="canvas">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={onNodeClick}
          onPaneClick={() => setSelectedNodeId(null)}
          fitView
          nodesDraggable={true}
          nodesConnectable={false}
          elementsSelectable={true}
          edgesUpdatable={false}
          edgesFocusable={false}
          deleteKeyCode={null}
        >
          <Background gap={16} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>

      <aside className="props-panel">
        {error && <div className="error">{error}</div>}
        {!selectedStructureNode && (
          <div className="props-empty">
            <div className="props-empty-title">ノードをクリックして編集</div>
            <div className="props-empty-hint">
              <span className="props-empty-mark">✏︎</span> マークの付いたノードは既定値から変更されています。
            </div>
          </div>
        )}

        {selectedStructureNode && selectedStructureNode.type === 'input' && (
          <>
            <div className="props-header">
              <span className="props-id">{selectedStructureNode.id}</span>
              <span className="props-type-badge">input</span>
            </div>
            <p className="props-readonly-note">
              入力ノードは log_text を下流に流す固定機能です。編集項目はありません。
            </p>
          </>
        )}

        {selectedStructureNode && selectedStructureNode.type === 'static' && (
          <>
            <div className="props-header">
              <span className="props-id">{selectedStructureNode.id}</span>
              <span className="props-type-badge">deterministic</span>
            </div>
            <p className="props-readonly-note">
              {selectedStructureNode.label}: ルールベース処理（Python コードで実装、UI 編集不可）。
            </p>
          </>
        )}

        {selectedStructureNode && selectedSlot && (
          <>
            <div className="props-header">
              <span className="props-id">{selectedStructureNode.id}</span>
              <span className="props-type-badge">{selectedStructureNode.type}</span>
              {modifiedSlotIds.has(selectedSlot.slot_id) && (
                <span className="modified-badge">変更あり</span>
              )}
            </div>
            <div className="props-subheader">
              slot: <code>{selectedSlot.slot_id}</code>
              {selectedStructureNode.type === 'slot_instance' && (
                <span className="props-shared-note">
                  （プロンプトは並列モデル間で共有、モデルは固定）
                </span>
              )}
            </div>

            <label className="props-field">
              モデル
              {selectedStructureNode.fixed_model ? (
                <span className="slot-model-fixed">
                  {selectedStructureNode.fixed_model}（並列実行のため固定）
                </span>
              ) : selectedSlot.allowed_models.length === 0 ? (
                <span className="slot-model-fixed">
                  {selectedSlot.default_model}（固定）
                </span>
              ) : (
                <select
                  value={modelOverrides[selectedSlot.slot_id] ?? selectedSlot.default_model}
                  onChange={e => onModelChange(selectedSlot.slot_id, e.target.value)}
                  disabled={disabled}
                >
                  {selectedSlot.allowed_models.map(m => (
                    <option key={m} value={m}>
                      {m}
                      {m === selectedSlot.default_model ? '（既定）' : ''}
                    </option>
                  ))}
                </select>
              )}
            </label>

            <label className="props-field">
              System Prompt
              <textarea
                value={promptOverrides[selectedSlot.slot_id] ?? selectedSlot.default_prompt}
                onChange={e => onPromptChange(selectedSlot.slot_id, e.target.value)}
                disabled={disabled}
                rows={16}
                spellCheck={false}
              />
            </label>
          </>
        )}
      </aside>
    </div>
  )
}

export function BuiltinConfigCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  )
}
