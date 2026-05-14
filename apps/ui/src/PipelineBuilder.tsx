import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactFlow, {
  Background,
  Controls,
  ReactFlowProvider,
  addEdge,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeMouseHandler,
} from 'reactflow'
import 'reactflow/dist/style.css'
import type {
  AnalysisResult,
  ConfigEntry,
  LogEntry,
  NodeTypeDef,
  NodeTypesResponse,
  PipelineDef,
  PipelineEdge,
  PipelineNode,
  PipelineNodeType,
  SavedConfigDTO,
} from './types'
import { layoutWithDagre } from './dagreLayout'

const API_BASE = 'http://localhost:8000'

interface RFNodeData {
  type: PipelineNodeType
  prompt?: string
  model?: string
  input_template?: string
  fixed: boolean
  label: string
}

interface Props {
  configList: ConfigEntry[]
  logs: LogEntry[]
  selectedLog: string
  onSelectedLogChange: (log: string) => void
  onConfigsRefresh: () => Promise<ConfigEntry[]>
  // 編集対象。null なら新規作成、user:N なら既存読み込み
  editingConfigId: string | null
  onEditingConfigIdChange: (id: string | null) => void
}

// ─── ノードラベル / スタイル ───────────────────────────────────────────

const ROLE_STYLES: Record<PipelineNodeType, { bg: string; border: string }> = {
  input: { bg: '#fef3c7', border: '#f59e0b' },
  llm: { bg: '#dbeafe', border: '#3b82f6' },
  output: { bg: '#fce7f3', border: '#db2777' },
}

function makeRFNode(node: PipelineNode, fixed: boolean): Node<RFNodeData> {
  const style = ROLE_STYLES[node.type]
  return {
    id: node.id,
    position: node.position ?? { x: 50, y: 100 },
    data: {
      type: node.type,
      prompt: node.prompt,
      model: node.model,
      input_template: node.input_template,
      fixed,
      label: node.id,
    },
    style: {
      background: style.bg,
      border: `2px solid ${style.border}`,
      borderRadius: 6,
      padding: '8px 12px',
      minWidth: 160,
      fontSize: 12,
    },
    deletable: !fixed,
  }
}

// ─── PipelineDef ↔ ReactFlow nodes/edges 変換 ──────────────────────────

function pipelineToFlow(
  pd: PipelineDef,
  nodeTypeDefs: NodeTypeDef[],
): { nodes: Node<RFNodeData>[]; edges: Edge[] } {
  const fixedTypes = new Set(nodeTypeDefs.filter(t => t.fixed).map(t => t.type))
  const positions = autoLayout(pd)
  const nodes = pd.nodes.map(n => {
    const withPos = { ...n, position: n.position ?? positions[n.id] }
    const fixed = fixedTypes.has(n.type)
    const rfNode = makeRFNode(withPos, fixed)
    rfNode.data.label = labelFor(n, fixed)
    return rfNode
  })
  const edges: Edge[] = pd.edges.map((e, i) => ({
    id: `e_${e.source}_${e.target}_${i}`,
    source: e.source,
    target: e.target,
    type: 'smoothstep',
    style: { stroke: '#94a3b8', strokeWidth: 1.5 },
  }))
  return { nodes, edges }
}

function flowToPipeline(
  nodes: Node<RFNodeData>[],
  edges: Edge[],
): PipelineDef {
  return {
    nodes: nodes.map<PipelineNode>(n => ({
      id: n.id,
      type: n.data.type,
      prompt: n.data.prompt,
      model: n.data.model,
      input_template: n.data.input_template,
      position: n.position,
    })),
    edges: edges.map<PipelineEdge>(e => ({ source: e.source, target: e.target })),
  }
}

function labelFor(node: PipelineNode, fixed: boolean): string {
  if (fixed) return `🔒 ${node.id} (${node.type})`
  return `${node.id} (${node.type})`
}

function autoLayout(pd: PipelineDef): Record<string, { x: number; y: number }> {
  return layoutWithDagre(pd.nodes, pd.edges, {
    nodeWidth: 220,
    nodeHeight: 90,
    rankdir: 'LR',
    nodesep: 60,
    ranksep: 120,
  })
}

// ─── ID 生成 ──────────────────────────────────────────────────────────

function genId(type: PipelineNodeType, existingIds: Set<string>): string {
  if (type === 'input' || type === 'output') return type
  let i = 1
  while (existingIds.has(`${type}_${i}`)) i += 1
  return `${type}_${i}`
}

const RESERVED_NODE_NAMES = new Set(['input', 'output', '__upstream__'])
const NODE_NAME_PATTERN = /^[a-zA-Z][a-zA-Z0-9_]*$/

function validateNodeName(name: string, existingIds: Set<string>): string | null {
  const trimmed = name.trim()
  if (!trimmed) return 'ノード名は必須です'
  if (!NODE_NAME_PATTERN.test(trimmed)) {
    return '英字で始まり、英数字とアンダースコアのみ使用可'
  }
  if (RESERVED_NODE_NAMES.has(trimmed)) {
    return `"${trimmed}" は予約語（input / output / __upstream__）です`
  }
  if (existingIds.has(trimmed)) {
    return `"${trimmed}" は既に使われています`
  }
  return null
}

// ─── 内部 Builder（ReactFlowProvider の中で動く） ─────────────────────

function PipelineBuilderInner(props: Props) {
  const {
    configList,
    logs,
    selectedLog,
    onSelectedLogChange,
    onConfigsRefresh,
    editingConfigId,
    onEditingConfigIdChange,
  } = props

  const [nodes, setNodes, onNodesChange] = useNodesState<RFNodeData>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [pipelineName, setPipelineName] = useState<string>('')
  const [savingState, setSavingState] = useState<'idle' | 'saving'>('idle')
  const [error, setError] = useState<string | null>(null)

  const [running, setRunning] = useState<boolean>(false)
  const [elapsedSec, setElapsedSec] = useState<number>(0)
  const [runResult, setRunResult] = useState<AnalysisResult | null>(null)
  const [runError, setRunError] = useState<string | null>(null)

  const [nodeTypeDefs, setNodeTypeDefs] = useState<NodeTypeDef[]>([])
  const [allowedModels, setAllowedModels] = useState<string[]>([])

  const reactFlowWrapper = useRef<HTMLDivElement>(null)
  const { screenToFlowPosition } = useReactFlow()

  const editingUserId = editingConfigId?.startsWith('user:') ? Number(editingConfigId.split(':')[1]) : null

  const editableTypes = useMemo(
    () => nodeTypeDefs.filter(t => !t.fixed),
    [nodeTypeDefs],
  )

  // 起動時: nodeTypeDefs と default pipeline 取得
  useEffect(() => {
    fetch(`${API_BASE}/api/node-types`)
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: NodeTypesResponse) => {
        setNodeTypeDefs(d.node_types)
        setAllowedModels(d.allowed_models)
      })
      .catch(e => setError(`node-types 取得失敗: ${e.message}`))
  }, [])

  // editingConfigId が変わった時の reload
  const lastLoadedRef = useRef<string | null>(null)
  useEffect(() => {
    if (nodeTypeDefs.length === 0) return
    if (lastLoadedRef.current === editingConfigId) return
    lastLoadedRef.current = editingConfigId

    if (editingConfigId === null || editingConfigId === '__new__') {
      // 新規作成: default pipeline を取得
      fetch(`${API_BASE}/api/pipelines/default`)
        .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d: { pipeline: PipelineDef }) => {
          const { nodes: rfn, edges: rfe } = pipelineToFlow(d.pipeline, nodeTypeDefs)
          setNodes(rfn)
          setEdges(rfe)
          setPipelineName('')
          setSelectedNodeId(null)
          setRunResult(null)
          setRunError(null)
        })
        .catch(e => setError(`既定パイプライン取得失敗: ${e.message}`))
      return
    }

    if (editingUserId !== null) {
      // 既存読み込み
      fetch(`${API_BASE}/api/configs/saved`)
        .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d: { configs: SavedConfigDTO[] }) => {
          const sc = d.configs.find(c => c.id === editingUserId)
          if (!sc) throw new Error(`saved config id=${editingUserId} not found`)
          if (sc.base_config !== 'config5' || !sc.pipeline) {
            throw new Error('この構成は config5 (pipeline) ではないため構成設計タブで編集不可')
          }
          const { nodes: rfn, edges: rfe } = pipelineToFlow(sc.pipeline, nodeTypeDefs)
          setNodes(rfn)
          setEdges(rfe)
          setPipelineName(sc.name)
          setSelectedNodeId(null)
          setRunResult(null)
          setRunError(null)
        })
        .catch(e => setError(`構成読み込み失敗: ${e.message}`))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editingConfigId, nodeTypeDefs])

  // 実行中の経過秒数
  useEffect(() => {
    if (!running) {
      setElapsedSec(0)
      return
    }
    const start = Date.now()
    const id = setInterval(() => setElapsedSec(Math.floor((Date.now() - start) / 1000)), 500)
    return () => clearInterval(id)
  }, [running])

  // ─── ノード追加（ドラッグ＆ドロップ） ─────────────────────────────────

  const onDragStart = (event: React.DragEvent, nodeType: PipelineNodeType) => {
    event.dataTransfer.setData('application/x-pipeline-node-type', nodeType)
    event.dataTransfer.effectAllowed = 'move'
  }

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
  }, [])

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault()
      const type = event.dataTransfer.getData('application/x-pipeline-node-type') as PipelineNodeType
      if (!type) return
      const def = nodeTypeDefs.find(t => t.type === type)
      if (!def) return
      const existingIds = new Set(nodes.map(n => n.id))
      if (def.fixed && existingIds.has(type)) {
        setError(`${type} ノードは 1 つしか配置できません`)
        return
      }

      let id: string
      if (def.fixed) {
        id = genId(type, existingIds)
      } else {
        // ユーザーにノード名を入れさせる（input_template で {名前} として参照される）
        const suggested = genId(type, existingIds)
        const userInput = window.prompt(
          `ノード名を入力してください。\n他のノードの input_template で {ノード名} として参照されます。\n（英字始まり、英数字＋アンダースコアのみ）`,
          suggested,
        )
        if (userInput === null) return  // キャンセル
        const errMsg = validateNodeName(userInput, existingIds)
        if (errMsg) {
          setError(errMsg)
          return
        }
        id = userInput.trim()
      }

      const position = screenToFlowPosition({ x: event.clientX, y: event.clientY })
      const newNode: Node<RFNodeData> = {
        id,
        position,
        data: {
          type,
          prompt: def.default_prompt ?? undefined,
          model: def.default_model ?? undefined,
          input_template: def.default_input_template ?? undefined,
          fixed: def.fixed,
          label: `${id} (${type})`,
        },
        style: {
          background: ROLE_STYLES[type].bg,
          border: `2px solid ${ROLE_STYLES[type].border}`,
          borderRadius: 6,
          padding: '8px 12px',
          minWidth: 160,
          fontSize: 12,
        },
        deletable: !def.fixed,
      }
      setNodes(prev => [...prev, newNode])
    },
    [nodeTypeDefs, nodes, screenToFlowPosition, setNodes],
  )

  const onConnect = useCallback(
    (params: Connection) => {
      if (!params.source || !params.target) return
      // 自己ループ拒否
      if (params.source === params.target) return
      // 重複 edge 拒否
      if (edges.some(e => e.source === params.source && e.target === params.target)) return
      setEdges(prev =>
        addEdge(
          { ...params, type: 'smoothstep', style: { stroke: '#94a3b8', strokeWidth: 1.5 } },
          prev,
        ),
      )
    },
    [edges, setEdges],
  )

  const onNodeClick: NodeMouseHandler = useCallback((_, node) => {
    setSelectedNodeId(node.id)
  }, [])

  // ノード data 更新（プロパティペインからの入力反映）
  const updateNodeData = (nodeId: string, patch: Partial<RFNodeData>) => {
    setNodes(prev =>
      prev.map(n =>
        n.id === nodeId ? { ...n, data: { ...n.data, ...patch } } : n,
      ),
    )
  }

  const selectedNode = selectedNodeId ? nodes.find(n => n.id === selectedNodeId) : null

  // ─── 保存・実行 ────────────────────────────────────────────────────

  const buildPipeline = (): PipelineDef => flowToPipeline(nodes, edges)

  const validateLocal = (pd: PipelineDef): string | null => {
    const types = pd.nodes.map(n => n.type)
    const inCount = types.filter(t => t === 'input').length
    const outCount = types.filter(t => t === 'output').length
    if (inCount !== 1) return `input ノードは 1 つ必要（現在 ${inCount}）`
    if (outCount !== 1) return `output ノードは 1 つ必要（現在 ${outCount}）`
    // output に到達するパスが少なくとも 1 本あるか
    const ids = new Set(pd.nodes.map(n => n.id))
    for (const e of pd.edges) {
      if (!ids.has(e.source) || !ids.has(e.target)) return `不正な edge: ${e.source} -> ${e.target}`
    }
    return null
  }

  const handleSave = async () => {
    setError(null)
    if (!pipelineName.trim()) {
      setError('構成名を入力してください')
      return
    }
    const pipeline = buildPipeline()
    const v = validateLocal(pipeline)
    if (v) {
      setError(v)
      return
    }
    setSavingState('saving')
    try {
      let saved: SavedConfigDTO
      if (editingUserId !== null) {
        const r = await fetch(`${API_BASE}/api/configs/saved/${editingUserId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ overrides: {}, model_overrides: {}, pipeline }),
        })
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
        saved = await r.json()
      } else {
        const r = await fetch(`${API_BASE}/api/configs/saved`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: pipelineName,
            base_config: 'config5',
            overrides: {},
            model_overrides: {},
            pipeline,
          }),
        })
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
        saved = await r.json()
      }
      await onConfigsRefresh()
      onEditingConfigIdChange(`user:${saved.id}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSavingState('idle')
    }
  }

  const handleDelete = async () => {
    if (editingUserId === null) return
    if (!confirm(`構成 "${pipelineName}" を削除しますか?`)) return
    try {
      const r = await fetch(`${API_BASE}/api/configs/saved/${editingUserId}`, {
        method: 'DELETE',
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      await onConfigsRefresh()
      onEditingConfigIdChange('__new__')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const handleNew = () => {
    onEditingConfigIdChange('__new__')
  }

  const handleRun = async () => {
    setRunError(null)
    setRunResult(null)
    const pipeline = buildPipeline()
    const v = validateLocal(pipeline)
    if (v) {
      setRunError(v)
      return
    }
    if (!selectedLog) {
      setRunError('ログを選択してください')
      return
    }
    setRunning(true)
    try {
      const r = await fetch(`${API_BASE}/api/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          log_name: selectedLog,
          config: 'config5',
          pipeline,
        }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const data: AnalysisResult = await r.json()
      setRunResult(data)
    } catch (e) {
      setRunError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  const userPipelineConfigs = configList.filter(c => c.type === 'user' && c.base_config === 'config5')

  return (
    <div className="builder-root">
      <div className="builder-toolbar">
        <label className="builder-field">
          編集対象
          <select
            value={editingConfigId ?? '__new__'}
            onChange={e => onEditingConfigIdChange(e.target.value)}
            disabled={running || savingState === 'saving'}
          >
            <option value="__new__">— 新規作成 —</option>
            {userPipelineConfigs.map(c => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <button onClick={handleNew} disabled={running || savingState === 'saving'} className="btn-secondary">
          新規
        </button>
        <label className="builder-field grow">
          構成名
          <input
            type="text"
            placeholder="例: my-fw-app-pipeline"
            value={pipelineName}
            onChange={e => setPipelineName(e.target.value)}
            disabled={running || savingState === 'saving'}
          />
        </label>
        <button
          onClick={handleSave}
          disabled={running || savingState === 'saving' || !pipelineName.trim()}
          className="btn-primary"
        >
          {savingState === 'saving' ? '保存中…' : editingUserId !== null ? '上書き保存' : '保存'}
        </button>
        {editingUserId !== null && (
          <button onClick={handleDelete} disabled={running || savingState === 'saving'} className="btn-delete">
            削除
          </button>
        )}
      </div>

      <div className="builder-toolbar">
        <label className="builder-field grow">
          テスト実行ログ
          <select
            value={selectedLog}
            onChange={e => onSelectedLogChange(e.target.value)}
            disabled={running}
          >
            {logs.map(l => (
              <option key={l.name} value={l.name}>
                {l.name}（{l.lines} 行 / {l.bytes.toLocaleString()} bytes）
              </option>
            ))}
          </select>
        </label>
        <button
          onClick={handleRun}
          disabled={running || !selectedLog}
          className="btn-primary"
        >
          {running ? `実行中… ${elapsedSec}s` : 'プレビュー実行'}
        </button>
      </div>

      {error && <div className="error">構成: {error}</div>}
      {runError && <div className="error">実行: {runError}</div>}

      <div className="builder-canvas-wrap">
        <aside className="palette">
          <div className="palette-title">ノードパレット</div>
          <div className="palette-hint">ドラッグしてキャンバスに配置</div>
          {editableTypes.map(t => (
            <div
              key={t.type}
              className="palette-item"
              draggable
              onDragStart={e => onDragStart(e, t.type)}
              style={{
                background: ROLE_STYLES[t.type].bg,
                borderColor: ROLE_STYLES[t.type].border,
              }}
            >
              <div className="palette-item-label">{t.label}</div>
              <div className="palette-item-desc">{t.description}</div>
            </div>
          ))}
          <div className="palette-fixed-note">
            🔒 input / output は固定で削除不可。新規作成時に自動配置されます。
          </div>
        </aside>

        <div className="canvas" ref={reactFlowWrapper}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={onNodeClick}
            onPaneClick={() => setSelectedNodeId(null)}
            onDrop={onDrop}
            onDragOver={onDragOver}
            fitView
            deleteKeyCode={['Delete', 'Backspace']}
          >
            <Background gap={16} />
            <Controls />
          </ReactFlow>
        </div>

        {selectedNode && (
          <aside className="props-panel">
            <div className="props-header">
              <span className="props-id">{selectedNode.id}</span>
              <span className="props-type-badge">{selectedNode.data.type}</span>
              {selectedNode.data.fixed && <span className="props-fixed">固定</span>}
            </div>
            {selectedNode.data.type === 'input' ? (
              <p className="props-readonly-note">
                入力ノードは log_text を下流に流す固定機能です。編集項目はありません。
              </p>
            ) : (
              <>
                <label className="props-field">
                  モデル
                  <select
                    value={selectedNode.data.model ?? ''}
                    onChange={e => updateNodeData(selectedNode.id, { model: e.target.value })}
                    disabled={running || savingState === 'saving'}
                  >
                    {allowedModels.map(m => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="props-field">
                  System Prompt
                  <textarea
                    value={selectedNode.data.prompt ?? ''}
                    onChange={e => updateNodeData(selectedNode.id, { prompt: e.target.value })}
                    disabled={running || savingState === 'saving'}
                    rows={12}
                    spellCheck={false}
                  />
                </label>
                <label className="props-field">
                  入力テンプレート
                  <textarea
                    value={selectedNode.data.input_template ?? ''}
                    onChange={e =>
                      updateNodeData(selectedNode.id, { input_template: e.target.value })
                    }
                    disabled={running || savingState === 'saving'}
                    rows={6}
                    spellCheck={false}
                    placeholder="{input} = 元ログ、{<node_id>} = 上流ノードの出力"
                  />
                  <div className="props-hint">
                    プレースホルダ: <code>{'{input}'}</code> 元ログ、<code>{'{<node_id>}'}</code>{' '}
                    上流ノード出力、<code>{'{__upstream__}'}</code> 上流全部
                  </div>
                </label>
              </>
            )}
            <button onClick={() => setSelectedNodeId(null)} className="btn-secondary">
              閉じる
            </button>
          </aside>
        )}
      </div>

      {runResult && (
        <section className="builder-result">
          <h3>プレビュー実行結果</h3>
          <div className="builder-result-summary">
            <div>
              <strong>確信度</strong>: {runResult.confidence.toFixed(2)}
            </div>
            <div>
              <strong>tokens</strong>: {runResult.metrics.tokens_in.toLocaleString()} /{' '}
              {runResult.metrics.tokens_out.toLocaleString()}
            </div>
            <div>
              <strong>レイテンシ</strong>: {(runResult.metrics.latency_ms_total / 1000).toFixed(1)}s
            </div>
            <div>
              <strong>top</strong>:{' '}
              <span className={`badge cat-${runResult.root_cause_candidates[0]?.category ?? 'Unknown'}`}>
                {runResult.root_cause_candidates[0]?.category ?? '?'}
              </span>{' '}
              {runResult.root_cause_candidates[0]?.summary?.slice(0, 80) ?? '(no candidates)'}
            </div>
          </div>
          <details>
            <summary>詳細 JSON</summary>
            <pre className="builder-result-json">{JSON.stringify(runResult, null, 2)}</pre>
          </details>
        </section>
      )}
    </div>
  )
}

export function PipelineBuilder(props: Props) {
  return (
    <ReactFlowProvider>
      <PipelineBuilderInner {...props} />
    </ReactFlowProvider>
  )
}
