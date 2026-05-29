/**
 * トポロジー解析タブ。
 *
 * フロー:
 *   1. 画像（PNG/SVG）をアップロード → data URL として localStorage に保存
 *   2. 画像上にドラッグで矩形を描画 → ノード（id/type/label/ip）を定義
 *   3. ノードを選択 → サイドパネルでログを貼り付け（or samples/logs/ から選択）
 *   4. 「解析実行」→ POST /api/runs/topology-stream に SSE リクエスト
 *   5. 完了後、AnalysisResult.suspected_node_ids に含まれるノード矩形を赤系でハイライト
 *
 * 既存の構成4 SSE エンドポイントと同じイベント形式を返すので、RealtimeStreamView と
 * 同じパーサ ([App.tsx](./App.tsx)) を流用できる。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { AuditReportView } from './AuditReportView'
import { ChatHistoryView } from './ChatHistoryView'
import { ChatInput } from './ChatInput'
import { ConfirmationModal } from './ConfirmationModal'
import { DelegationHistoryView } from './DelegationHistoryView'
import { LiveChatView } from './LiveChatView'
import { RoundMetricsView } from './RoundMetricsView'
import { ViewModeToggle } from './ViewModeToggle'
import { GraphView } from './GraphView'
import { QuestionnairePanel } from './QuestionnairePanel'
import { TerraformImporter } from './TerraformImporter'
import type {
  AnalysisResult,
  ConfigEntry,
  DelegationEvent,
  LogEntry,
  NodeAttachment,
  NodeAttachments,
  QuestionnaireAnswers,
  SSEEvent,
  SuspectedNodeFinding,
  TopologyDef,
  TopologyNode,
} from './types'

const API_BASE = 'http://localhost:8000'
const STORAGE_KEY = 'log-analyzer.topology-v1'
const MAX_IMAGE_BYTES = 5 * 1024 * 1024 // 5MB

interface TopologyAnalysisProps {
  configList: ConfigEntry[]
  logs: LogEntry[]
  // 既存 App.tsx の SSE パーサとイベントレンダラを共用するため、App から渡す
  parseSSE: (response: Response) => AsyncGenerator<SSEEvent>
  renderEventSummary: (ev: SSEEvent) => React.ReactNode
  langfuseHost: string | null
}

// rally ベースの構成（config4 / config4 派生の user）だけを選べる
function isRallyConfig(c: ConfigEntry): boolean {
  return c.base_config === 'config4'
}

const EMPTY_TOPOLOGY: TopologyDef = {
  image: null,
  imageWidth: 0,
  imageHeight: 0,
  nodes: [],
  links: [],
}

function loadTopology(): TopologyDef {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return EMPTY_TOPOLOGY
    const parsed = JSON.parse(raw) as Partial<TopologyDef>
    return {
      image: parsed.image ?? null,
      imageWidth: parsed.imageWidth ?? 0,
      imageHeight: parsed.imageHeight ?? 0,
      nodes: Array.isArray(parsed.nodes) ? parsed.nodes : [],
      links: Array.isArray(parsed.links) ? parsed.links : [],
    }
  } catch {
    return EMPTY_TOPOLOGY
  }
}

function saveTopology(t: TopologyDef) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(t))
  } catch {
    // 容量超過時は silently fall back
  }
}

function uniqueNodeId(existing: TopologyNode[], base: string): string {
  if (!existing.some(n => n.id === base)) return base
  for (let i = 2; i < 1000; i++) {
    const candidate = `${base}-${i}`
    if (!existing.some(n => n.id === candidate)) return candidate
  }
  return `${base}-${Date.now()}`
}

// 画像上の座標 → 正規化座標
function toNormalized(
  px: number,
  py: number,
  rect: DOMRect,
): { x: number; y: number } {
  return {
    x: Math.max(0, Math.min(1, (px - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (py - rect.top) / rect.height)),
  }
}

export function TopologyAnalysis({
  configList,
  logs,
  parseSSE,
  renderEventSummary,
  langfuseHost,
}: TopologyAnalysisProps) {
  // ─── 永続化されるトポロジー定義 ───────────────────────────────
  const [topology, setTopology] = useState<TopologyDef>(() => loadTopology())
  useEffect(() => {
    saveTopology(topology)
  }, [topology])

  // ─── ノード別添付ファイル (永続化しない: 本文は揮発させる) ───────
  // 1 ノードに複数のログ / 設定ファイルを {name, content} として持てる
  const [nodeLogs, setNodeLogs] = useState<NodeAttachments>({})
  const [nodeConfigs, setNodeConfigs] = useState<NodeAttachments>({})
  // 問診票回答 (Phase B、揮発)
  const [questionnaireAnswers, setQuestionnaireAnswers] = useState<QuestionnaireAnswers>({})

  // ─── 編集状態 ─────────────────────────────────────────────
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [editMode, setEditMode] = useState<'select' | 'add'>('select')
  const [drawing, setDrawing] = useState<{
    startX: number
    startY: number
    curX: number
    curY: number
  } | null>(null)

  // 解析実行用
  const rallyConfigs = useMemo(() => configList.filter(isRallyConfig), [configList])
  const [selectedConfig, setSelectedConfig] = useState<string>('')
  useEffect(() => {
    if (!selectedConfig && rallyConfigs.length > 0) {
      setSelectedConfig(rallyConfigs[0].id)
    }
  }, [rallyConfigs, selectedConfig])
  const [rallyMaxRounds, setRallyMaxRounds] = useState<number>(3)
  // 監査エージェント (Phase C): integrator 後に GPT で独立検証するか
  const [auditAfterIntegrator, setAuditAfterIntegrator] = useState<boolean>(false)
  // 表示モード (Phase E): デフォルトをチャットに (議事録の UI 要求)
  const [viewMode, setViewMode] = useState<'standard' | 'chat'>('chat')
  // Terraform 一括取込モーダルの開閉
  const [tfImporterOpen, setTfImporterOpen] = useState(false)

  const [running, setRunning] = useState(false)
  const [streamEvents, setStreamEvents] = useState<SSEEvent[]>([])
  const [result, setResult] = useState<AnalysisResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  // rally_max_rounds 到達時の継続/停止モーダル
  const [runId, setRunId] = useState<string | null>(null)
  const [decisionBusy, setDecisionBusy] = useState(false)
  const [pendingConfirmation, setPendingConfirmation] = useState<{
    round: number
    rally_max_rounds: number
    delegation_history: DelegationEvent[]
  } | null>(null)

  const imageRef = useRef<HTMLImageElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)

  const suspectedSet = useMemo(
    () => new Set(result?.suspected_node_ids ?? []),
    [result],
  )
  // ノードID → severity ("primary" | "secondary" | "info" | "")。
  // ハイライト色とラベル色をここから引く。
  const severityById = useMemo(() => {
    const m = new Map<string, string>()
    for (const f of result?.suspected_node_findings ?? []) {
      m.set(f.node_id, f.severity || '')
    }
    return m
  }, [result])
  // 矩形に付ける highlight クラス:
  //   - "info":       ハイライトしない
  //   - "secondary":  橙 (静止)
  //   - "primary" / 不明: 赤 (点滅)
  const highlightClass = useCallback(
    (nodeId: string): string => {
      if (!suspectedSet.has(nodeId)) return ''
      const sev = severityById.get(nodeId) ?? ''
      if (sev === 'info') return ''
      if (sev === 'secondary') return 'is-suspected sev-secondary'
      return 'is-suspected sev-primary'
    },
    [suspectedSet, severityById],
  )

  // ─── 画像アップロード ───────────────────────────────────────
  const handleImageFile = useCallback((file: File) => {
    if (file.size > MAX_IMAGE_BYTES) {
      setError(`画像は ${MAX_IMAGE_BYTES / 1024 / 1024}MB 以下にしてください (今: ${(file.size / 1024 / 1024).toFixed(2)}MB)`)
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = String(reader.result)
      // 画像のネイティブ寸法を取得
      const img = new Image()
      img.onload = () => {
        setTopology(t => ({
          ...t,
          image: dataUrl,
          imageWidth: img.naturalWidth,
          imageHeight: img.naturalHeight,
        }))
        setError(null)
      }
      img.onerror = () => setError('画像のデコードに失敗しました')
      img.src = dataUrl
    }
    reader.onerror = () => setError('画像の読み込みに失敗しました')
    reader.readAsDataURL(file)
  }, [])

  const onImageInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (f) handleImageFile(f)
    e.target.value = ''
  }

  // ─── 矩形描画 (add モード) ─────────────────────────────────
  const onCanvasMouseDown = (e: React.MouseEvent) => {
    if (editMode !== 'add' || !topology.image) return
    const rect = containerRef.current?.getBoundingClientRect()
    if (!rect) return
    const { x, y } = toNormalized(e.clientX, e.clientY, rect)
    setDrawing({ startX: x, startY: y, curX: x, curY: y })
  }
  const onCanvasMouseMove = (e: React.MouseEvent) => {
    if (!drawing) return
    const rect = containerRef.current?.getBoundingClientRect()
    if (!rect) return
    const { x, y } = toNormalized(e.clientX, e.clientY, rect)
    setDrawing(d => (d ? { ...d, curX: x, curY: y } : null))
  }
  const onCanvasMouseUp = () => {
    if (!drawing) return
    const x = Math.min(drawing.startX, drawing.curX)
    const y = Math.min(drawing.startY, drawing.curY)
    const w = Math.abs(drawing.curX - drawing.startX)
    const h = Math.abs(drawing.curY - drawing.startY)
    setDrawing(null)
    if (w < 0.01 || h < 0.01) return
    const newId = uniqueNodeId(topology.nodes, 'node')
    const newNode: TopologyNode = { id: newId, type: '', label: '', ip: '', x, y, w, h }
    setTopology(t => ({ ...t, nodes: [...t.nodes, newNode] }))
    setEditMode('select')
    setSelectedNodeId(newId)
  }

  // ─── ノード操作 ─────────────────────────────────────────
  const updateNode = (id: string, patch: Partial<TopologyNode>) => {
    setTopology(t => ({
      ...t,
      nodes: t.nodes.map(n => (n.id === id ? { ...n, ...patch } : n)),
    }))
  }
  const renameNode = (oldId: string, newId: string) => {
    const trimmed = newId.trim()
    if (!trimmed || trimmed === oldId) return
    setTopology(t => {
      if (t.nodes.some(n => n.id === trimmed)) return t // 重複は弾く
      return {
        ...t,
        nodes: t.nodes.map(n => (n.id === oldId ? { ...n, id: trimmed } : n)),
        links: t.links.map(l => ({
          source: l.source === oldId ? trimmed : l.source,
          target: l.target === oldId ? trimmed : l.target,
        })),
      }
    })
    const renameKey = (m: NodeAttachments): NodeAttachments => {
      if (!(oldId in m)) return m
      const next: NodeAttachments = {}
      for (const [k, val] of Object.entries(m)) {
        next[k === oldId ? trimmed : k] = val
      }
      return next
    }
    setNodeLogs(renameKey)
    setNodeConfigs(renameKey)
    if (selectedNodeId === oldId) setSelectedNodeId(trimmed)
  }
  const deleteNode = (id: string) => {
    setTopology(t => ({
      ...t,
      nodes: t.nodes.filter(n => n.id !== id),
      links: t.links.filter(l => l.source !== id && l.target !== id),
    }))
    const dropKey = (m: NodeAttachments): NodeAttachments => {
      if (!(id in m)) return m
      const next: NodeAttachments = {}
      for (const [k, val] of Object.entries(m)) {
        if (k !== id) next[k] = val
      }
      return next
    }
    setNodeLogs(dropKey)
    setNodeConfigs(dropKey)
    if (selectedNodeId === id) setSelectedNodeId(null)
  }
  const clearAll = () => {
    if (!confirm('構成図とノード定義をすべて削除します。よろしいですか？')) return
    setTopology(EMPTY_TOPOLOGY)
    setNodeLogs({})
    setNodeConfigs({})
    setSelectedNodeId(null)
    setResult(null)
    setStreamEvents([])
  }

  // 1 ノードに新規アタッチメントを追加 (logs / configs どちらにも使えるよう setter を切替)
  const addAttachment = (
    setter: Dispatch<SetStateAction<NodeAttachments>>,
    nodeId: string,
    initial: NodeAttachment,
  ) => {
    setter(prev => ({
      ...prev,
      [nodeId]: [...(prev[nodeId] ?? []), initial],
    }))
  }
  const updateAttachment = (
    setter: Dispatch<SetStateAction<NodeAttachments>>,
    nodeId: string,
    index: number,
    patch: Partial<NodeAttachment>,
  ) => {
    setter(prev => {
      const list = prev[nodeId] ?? []
      if (index < 0 || index >= list.length) return prev
      const next = [...list]
      next[index] = { ...next[index], ...patch }
      return { ...prev, [nodeId]: next }
    })
  }
  const removeAttachment = (
    setter: Dispatch<SetStateAction<NodeAttachments>>,
    nodeId: string,
    index: number,
  ) => {
    setter(prev => {
      const list = prev[nodeId] ?? []
      if (index < 0 || index >= list.length) return prev
      const next = list.filter((_, i) => i !== index)
      if (next.length === 0) {
        const { [nodeId]: _drop, ...rest } = prev
        return rest
      }
      return { ...prev, [nodeId]: next }
    })
  }
  const loadSampleIntoNode = async (id: string, sampleName: string) => {
    try {
      const r = await fetch(`${API_BASE}/api/logs/${encodeURIComponent(sampleName)}/content`)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const data = (await r.json()) as { content: string }
      // 新規ログとして 1 件追加 (既存にマージしない: 区別が崩れるため)
      addAttachment(setNodeLogs, id, { name: sampleName, content: data.content })
    } catch (e) {
      setError(`サンプルログの読み込みに失敗: ${(e as Error).message}`)
    }
  }

  // ─── 解析実行 ────────────────────────────────────────────
  // 添付の "実体のあるもの" だけを抜き出すヘルパ
  const filteredAttachments = useCallback(
    (m: NodeAttachments): NodeAttachments => {
      const out: NodeAttachments = {}
      for (const [nid, list] of Object.entries(m)) {
        const kept = list.filter(a => a.content.trim().length > 0)
        if (kept.length > 0) out[nid] = kept
      }
      return out
    },
    [],
  )
  const canRun = useMemo(() => {
    if (running) return false
    if (!selectedConfig) return false
    if (topology.nodes.length === 0) return false
    const hasLog = Object.values(nodeLogs).some(list => list.some(a => a.content.trim().length > 0))
    const hasCfg = Object.values(nodeConfigs).some(list => list.some(a => a.content.trim().length > 0))
    return hasLog || hasCfg
  }, [running, selectedConfig, topology.nodes, nodeLogs, nodeConfigs])

  const run = async () => {
    if (!canRun) return
    setRunning(true)
    setError(null)
    setResult(null)
    setStreamEvents([])
    const ctrl = new AbortController()
    abortRef.current = ctrl
    try {
      const body = {
        config: selectedConfig,
        rally_max_rounds: rallyMaxRounds,
        topology: {
          nodes: topology.nodes.map(n => ({
            id: n.id,
            type: n.type,
            label: n.label,
            ip: n.ip,
          })),
          links: topology.links,
        },
        node_logs: filteredAttachments(nodeLogs),
        node_configs: filteredAttachments(nodeConfigs),
        questionnaire_answers: questionnaireAnswers,
        audit_after_integrator: auditAfterIntegrator,
      }
      const r = await fetch(`${API_BASE}/api/runs/topology-stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      })
      if (!r.ok) {
        const text = await r.text()
        throw new Error(`HTTP ${r.status}: ${text}`)
      }
      for await (const ev of parseSSE(r)) {
        setStreamEvents(prev => [...prev, ev])
        if (ev.kind === 'run_id_assigned') {
          setRunId(String(ev.data.run_id ?? ''))
        } else if (ev.kind === 'await_confirmation') {
          // rally_max_rounds 到達 → 継続/停止モーダル
          setPendingConfirmation({
            round: Number(ev.data.round ?? 0),
            rally_max_rounds: Number(ev.data.rally_max_rounds ?? 0),
            delegation_history: (ev.data.delegation_history as DelegationEvent[]) ?? [],
          })
        } else if (ev.kind === 'user_decision') {
          // 自身応答受領 → モーダル閉じる
          setPendingConfirmation(null)
        } else if (ev.kind === 'final') {
          const res = ev.data.result as AnalysisResult | undefined
          if (res) setResult(res)
        } else if (ev.kind === 'error') {
          setError(String(ev.data.message ?? 'unknown stream error'))
        }
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        setError((e as Error).message)
      }
    } finally {
      setRunning(false)
      abortRef.current = null
    }
  }
  // rally_max_rounds 到達時の継続/停止 (await_confirmation 応答)
  const submitDecision = async (action: 'continue' | 'stop', extendBy?: number) => {
    if (!runId) return
    setDecisionBusy(true)
    try {
      const body: { action: string; extend_by?: number } = { action }
      if (action === 'continue' && extendBy) body.extend_by = extendBy
      const r = await fetch(`${API_BASE}/api/runs/${runId}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!r.ok) {
        const text = await r.text()
        throw new Error(`HTTP ${r.status}: ${text}`)
      }
    } catch (e) {
      setError(`decision 送信失敗: ${(e as Error).message}`)
    } finally {
      setDecisionBusy(false)
    }
  }

  const cancel = () => {
    abortRef.current?.abort()
  }

  // ─── レンダリング ────────────────────────────────────────
  const selectedNode = topology.nodes.find(n => n.id === selectedNodeId) ?? null

  return (
    <section className="topology-mode">
      <div className="topology-header">
        <h2>トポロジー解析</h2>
        <p className="muted">
          ネットワーク構成図を取り込み、各ノードにログを割り当てて解析する。完了後、障害に
          関与する可能性が高いノードがハイライト表示される。
        </p>
      </div>

      <div className="topology-toolbar">
        <label className="btn-file">
          画像を選択
          <input type="file" accept="image/png,image/jpeg,image/svg+xml" onChange={onImageInputChange} hidden />
        </label>
        <div className="toolbar-modes">
          <button
            className={editMode === 'select' ? 'tab active' : 'tab'}
            onClick={() => setEditMode('select')}
            disabled={!topology.image}
          >
            選択・編集
          </button>
          <button
            className={editMode === 'add' ? 'tab active' : 'tab'}
            onClick={() => setEditMode('add')}
            disabled={!topology.image}
          >
            ノード追加（ドラッグで矩形描画）
          </button>
        </div>
        <button
          className="btn-secondary"
          onClick={() => setTfImporterOpen(true)}
          disabled={topology.nodes.length === 0}
          title={topology.nodes.length === 0 ? 'ノードを先に作成してください' : ''}
        >
          Terraform 一括取込
        </button>
        <button className="btn-secondary" onClick={clearAll} disabled={!topology.image && topology.nodes.length === 0}>
          すべてクリア
        </button>
      </div>

      <div className="topology-canvas-row">
        <div
          ref={containerRef}
          className={`topology-canvas mode-${editMode}`}
          onMouseDown={onCanvasMouseDown}
          onMouseMove={onCanvasMouseMove}
          onMouseUp={onCanvasMouseUp}
          onMouseLeave={() => setDrawing(null)}
        >
          {topology.image ? (
            <>
              <img
                ref={imageRef}
                src={topology.image}
                alt="topology"
                className="topology-image"
                draggable={false}
              />
              {/* ノード矩形 + 描画中プレビュー */}
              <svg className="topology-overlay" viewBox="0 0 1 1" preserveAspectRatio="none">
                {topology.nodes.map(n => {
                  const isSelected = n.id === selectedNodeId
                  const hl = highlightClass(n.id)
                  return (
                    <g key={n.id}>
                      <rect
                        x={n.x}
                        y={n.y}
                        width={n.w}
                        height={n.h}
                        className={[
                          'node-rect',
                          isSelected ? 'is-selected' : '',
                          hl,
                        ].filter(Boolean).join(' ')}
                        onMouseDown={e => {
                          if (editMode === 'select') {
                            e.stopPropagation()
                            setSelectedNodeId(n.id)
                          }
                        }}
                        vectorEffect="non-scaling-stroke"
                      />
                      <text
                        x={n.x + 0.005}
                        y={n.y + 0.018}
                        className={['node-label', hl].filter(Boolean).join(' ')}
                      >
                        {n.id}{n.type ? ` [${n.type}]` : ''}
                      </text>
                    </g>
                  )
                })}
                {drawing && (
                  <rect
                    x={Math.min(drawing.startX, drawing.curX)}
                    y={Math.min(drawing.startY, drawing.curY)}
                    width={Math.abs(drawing.curX - drawing.startX)}
                    height={Math.abs(drawing.curY - drawing.startY)}
                    className="node-rect drawing"
                    vectorEffect="non-scaling-stroke"
                  />
                )}
              </svg>
            </>
          ) : (
            <div className="topology-empty">
              画像を選択してください（PNG / JPEG / SVG, 最大 5MB）
            </div>
          )}
        </div>

        <aside className="topology-sidebar">
          {selectedNode ? (
            <NodeEditor
              node={selectedNode}
              logs={nodeLogs[selectedNode.id] ?? []}
              configs={nodeConfigs[selectedNode.id] ?? []}
              sampleLogs={logs}
              onUpdate={(patch) => updateNode(selectedNode.id, patch)}
              onRename={(newId) => renameNode(selectedNode.id, newId)}
              onAddLog={() => addAttachment(setNodeLogs, selectedNode.id, { name: '', content: '' })}
              onUpdateLog={(i, patch) => updateAttachment(setNodeLogs, selectedNode.id, i, patch)}
              onRemoveLog={(i) => removeAttachment(setNodeLogs, selectedNode.id, i)}
              onAddConfig={() => addAttachment(setNodeConfigs, selectedNode.id, { name: '', content: '' })}
              onUpdateConfig={(i, patch) => updateAttachment(setNodeConfigs, selectedNode.id, i, patch)}
              onRemoveConfig={(i) => removeAttachment(setNodeConfigs, selectedNode.id, i)}
              onLoadSample={(name) => loadSampleIntoNode(selectedNode.id, name)}
              onDelete={() => deleteNode(selectedNode.id)}
              isSuspected={suspectedSet.has(selectedNode.id)}
              allNodes={topology.nodes}
            />
          ) : (
            <div className="sidebar-hint">
              {editMode === 'add'
                ? '画像上でドラッグしてノード矩形を描画'
                : 'ノード矩形をクリックして編集'}
              <NodeList
                nodes={topology.nodes}
                nodeLogs={nodeLogs}
                nodeConfigs={nodeConfigs}
                severityById={severityById}
                onSelect={(id) => setSelectedNodeId(id)}
              />
            </div>
          )}
        </aside>
      </div>

      {/* standard モード時のみ実行バー直前に表示 (chat モードはチャット内に統合) */}
      {viewMode === 'standard' && (
        <QuestionnairePanel
          answers={questionnaireAnswers}
          onAnswersChange={setQuestionnaireAnswers}
          disabled={running}
        />
      )}

      <div className="topology-run-bar">
        <label>
          構成:
          <select value={selectedConfig} onChange={e => setSelectedConfig(e.target.value)} disabled={running}>
            {rallyConfigs.length === 0 ? (
              <option value="">（config4 系の構成がありません）</option>
            ) : (
              rallyConfigs.map(c => (
                <option key={c.id} value={c.id}>{c.label}</option>
              ))
            )}
          </select>
        </label>
        <label>
          rally_max_rounds:
          <input
            type="number"
            min={1}
            max={20}
            value={rallyMaxRounds}
            onChange={e => setRallyMaxRounds(Math.max(1, Math.min(20, Number(e.target.value) || 3)))}
            disabled={running}
          />
        </label>
        <label className="audit-toggle">
          <input
            type="checkbox"
            checked={auditAfterIntegrator}
            onChange={e => setAuditAfterIntegrator(e.target.checked)}
            disabled={running}
          />
          <span>GPT 監査も実行</span>
        </label>
        <button onClick={run} disabled={!canRun} className="run-button">
          {running ? '解析中...' : 'トポロジー解析を実行'}
        </button>
        {running && <button onClick={cancel} className="btn-secondary">中止</button>}
      </div>

      {error && <div className="topology-error">エラー: {error}</div>}

      {/* 表示モード切替は常に表示 (chat モードでは問診票入力も含むため) */}
      <ViewModeToggle mode={viewMode} onChange={setViewMode} />

      {/* chat モード: 問診票 + ライブログ + 介入入力 を 1 セクションに統合 */}
      {viewMode === 'chat' && (
        <section className="realtime-stream chat-section">
          <h3>
            会話
            {streamEvents.length > 0 && (
              <span className="realtime-count">{streamEvents.length} イベント</span>
            )}
          </h3>
          <QuestionnairePanel
            answers={questionnaireAnswers}
            onAnswersChange={setQuestionnaireAnswers}
            disabled={running}
          />
          {streamEvents.length > 0 ? (
            <LiveChatView events={streamEvents} questionnaireAnswers={questionnaireAnswers} />
          ) : (
            <div className="live-chat-empty muted">実行を開始するとここに会話が表示されます。</div>
          )}
          <ChatInput runId={runId} disabled={!running} />
        </section>
      )}

      {/* 標準モードの従来表示 */}
      {viewMode === 'standard' && streamEvents.length > 0 && (
        <section className="realtime-stream">
          <h3>
            リアルタイム実行ログ
            <span className="realtime-count">{streamEvents.length} イベント</span>
          </h3>
          <ol className="stream-events">
            {streamEvents.map((ev, i) => (
              <li key={i} className={`stream-event kind-${ev.kind}`}>
                <span className="stream-kind">{ev.kind}</span>
                <span className="stream-body">{renderEventSummary(ev)}</span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {result && (
        viewMode === 'standard' ? (
          <TopologyResultView result={result} langfuseHost={langfuseHost} suspected={suspectedSet} topology={topology} />
        ) : (
          <section className="topology-result">
            <h3>解析結果 (チャット表示)</h3>
            <ChatHistoryView result={result} questionnaireAnswers={questionnaireAnswers} />
          </section>
        )
      )}

      {/* Terraform 一括取込モーダル */}
      {tfImporterOpen && (
        <TerraformImporter
          nodes={topology.nodes}
          onApply={(additions) => {
            // 既存添付を保持しつつ追記 (上書きしない、同名なら重複追加)
            setNodeConfigs(prev => {
              const next: NodeAttachments = { ...prev }
              for (const [nodeId, attaches] of Object.entries(additions)) {
                next[nodeId] = [...(next[nodeId] ?? []), ...attaches]
              }
              return next
            })
          }}
          onClose={() => setTfImporterOpen(false)}
        />
      )}

      {/* rally_max_rounds 到達時の継続/停止モーダル */}
      {pendingConfirmation && (
        <ConfirmationModal
          round={pendingConfirmation.round}
          maxRounds={pendingConfirmation.rally_max_rounds}
          history={pendingConfirmation.delegation_history}
          busy={decisionBusy}
          onContinue={(extendBy) => submitDecision('continue', extendBy)}
          onStop={() => submitDecision('stop')}
        />
      )}
    </section>
  )
}

interface NodeEditorProps {
  node: TopologyNode
  logs: NodeAttachment[]
  configs: NodeAttachment[]
  sampleLogs: LogEntry[]
  onUpdate: (patch: Partial<TopologyNode>) => void
  onRename: (newId: string) => void
  onAddLog: () => void
  onUpdateLog: (index: number, patch: Partial<NodeAttachment>) => void
  onRemoveLog: (index: number) => void
  onAddConfig: () => void
  onUpdateConfig: (index: number, patch: Partial<NodeAttachment>) => void
  onRemoveConfig: (index: number) => void
  onLoadSample: (name: string) => void
  onDelete: () => void
  isSuspected: boolean
  allNodes: TopologyNode[]
}

function NodeEditor({
  node,
  logs,
  configs,
  sampleLogs,
  onUpdate,
  onRename,
  onAddLog,
  onUpdateLog,
  onRemoveLog,
  onAddConfig,
  onUpdateConfig,
  onRemoveConfig,
  onLoadSample,
  onDelete,
  isSuspected,
  allNodes,
}: NodeEditorProps) {
  const [idDraft, setIdDraft] = useState(node.id)
  useEffect(() => setIdDraft(node.id), [node.id])
  const idTaken = idDraft !== node.id && allNodes.some(n => n.id === idDraft)
  return (
    <div className="node-editor">
      <div className="node-editor-header">
        <h3>ノード編集{isSuspected && <span className="node-suspected-pill">⚠ 障害候補</span>}</h3>
        <button className="btn-danger" onClick={onDelete}>削除</button>
      </div>
      <label className="field">
        <span>id</span>
        <input
          type="text"
          value={idDraft}
          onChange={e => setIdDraft(e.target.value)}
          onBlur={() => {
            if (!idTaken && idDraft !== node.id) onRename(idDraft)
            else setIdDraft(node.id)
          }}
        />
        {idTaken && <span className="field-error">同じ id が既にあります</span>}
      </label>
      <label className="field">
        <span>type (L2 / L3 / FW / Server / …)</span>
        <input type="text" value={node.type} onChange={e => onUpdate({ type: e.target.value })} />
      </label>
      <label className="field">
        <span>label</span>
        <input type="text" value={node.label} onChange={e => onUpdate({ label: e.target.value })} />
      </label>
      <label className="field">
        <span>ip</span>
        <input type="text" value={node.ip} onChange={e => onUpdate({ ip: e.target.value })} />
      </label>

      <AttachmentSection
        title="ログファイル"
        kind="log"
        items={logs}
        onAdd={onAddLog}
        onUpdate={onUpdateLog}
        onRemove={onRemoveLog}
        extraTopRow={
          <select
            defaultValue=""
            onChange={e => {
              if (e.target.value) {
                onLoadSample(e.target.value)
                e.target.value = ''
              }
            }}
          >
            <option value="">＋ samples/logs/ から追加...</option>
            {sampleLogs.map(l => (
              <option key={l.name} value={l.name}>{l.name} ({l.lines} 行)</option>
            ))}
          </select>
        }
      />

      <AttachmentSection
        title="設定ファイル (Config)"
        kind="config"
        items={configs}
        onAdd={onAddConfig}
        onUpdate={onUpdateConfig}
        onRemove={onRemoveConfig}
      />
    </div>
  )
}

interface AttachmentSectionProps {
  title: string
  kind: 'log' | 'config'
  items: NodeAttachment[]
  onAdd: () => void
  onUpdate: (index: number, patch: Partial<NodeAttachment>) => void
  onRemove: (index: number) => void
  extraTopRow?: React.ReactNode
}

function AttachmentSection({ title, kind, items, onAdd, onUpdate, onRemove, extraTopRow }: AttachmentSectionProps) {
  return (
    <div className={`attach-section attach-${kind}`}>
      <div className="attach-header">
        <h4>{title} <span className="attach-count">({items.length})</span></h4>
        <button type="button" className="btn-add-attach" onClick={onAdd}>＋ 追加</button>
      </div>
      {extraTopRow && <div className="attach-extra-row">{extraTopRow}</div>}
      {items.length === 0 && (
        <div className="attach-empty">（未設定）</div>
      )}
      {items.map((a, i) => (
        <div key={i} className="attach-item">
          <div className="attach-item-row">
            <input
              type="text"
              className="attach-name"
              placeholder={kind === 'log' ? 'ファイル名 (例: fw-syslog.log)' : 'ファイル名 (例: fw-policy.conf)'}
              value={a.name}
              onChange={e => onUpdate(i, { name: e.target.value })}
            />
            <button type="button" className="btn-danger btn-small" onClick={() => onRemove(i)}>削除</button>
          </div>
          <textarea
            className="attach-content"
            placeholder={kind === 'log' ? 'ログ内容を貼り付け' : '設定内容を貼り付け'}
            value={a.content}
            onChange={e => onUpdate(i, { content: e.target.value })}
            rows={6}
          />
        </div>
      ))}
    </div>
  )
}

interface NodeListProps {
  nodes: TopologyNode[]
  nodeLogs: NodeAttachments
  nodeConfigs: NodeAttachments
  severityById: Map<string, string>
  onSelect: (id: string) => void
}

function NodeList({ nodes, nodeLogs, nodeConfigs, severityById, onSelect }: NodeListProps) {
  if (nodes.length === 0) return null
  const countFilled = (list: NodeAttachment[] | undefined): number =>
    (list ?? []).reduce((n, a) => (a.content.trim().length > 0 ? n + 1 : n), 0)
  return (
    <ul className="node-list">
      {nodes.map(n => {
        const logCount = countFilled(nodeLogs[n.id])
        const cfgCount = countFilled(nodeConfigs[n.id])
        const sev = severityById.get(n.id) ?? ''
        const sevClass =
          sev === 'primary' ? 'sev-primary'
          : sev === 'secondary' ? 'sev-secondary'
          : severityById.has(n.id) && sev === '' ? 'sev-primary'
          : ''
        return (
          <li
            key={n.id}
            className={['node-list-item', sevClass].filter(Boolean).join(' ')}
            onClick={() => onSelect(n.id)}
          >
            <span className="node-list-id">{n.id}</span>
            {n.type && <span className="node-list-type">[{n.type}]</span>}
            <span className="node-list-counts">
              {logCount > 0 ? <span className="cnt-log">log×{logCount}</span> : null}
              {cfgCount > 0 ? <span className="cnt-cfg">cfg×{cfgCount}</span> : null}
              {logCount === 0 && cfgCount === 0 && <span className="cnt-empty">○</span>}
            </span>
          </li>
        )
      })}
    </ul>
  )
}

interface TopologyResultViewProps {
  result: AnalysisResult
  langfuseHost: string | null
  suspected: Set<string>
  topology: TopologyDef
}

function TopologyResultView({ result, langfuseHost, suspected, topology }: TopologyResultViewProps) {
  const traceUrl = langfuseHost ? `${langfuseHost}/trace/${result.trace_id}` : null

  // 各 suspected ノードについて、topology の id/type/label/ip と findings (summary/severity) をマージ
  const findingsById = useMemo(() => {
    const m = new Map<string, SuspectedNodeFinding>()
    for (const f of result.suspected_node_findings ?? []) m.set(f.node_id, f)
    return m
  }, [result.suspected_node_findings])
  const suspectedDetails = topology.nodes
    .filter(n => suspected.has(n.id))
    .map(n => ({ node: n, finding: findingsById.get(n.id) ?? null }))
  // severity 優先順 (primary → secondary → info → '') でソート
  const severityRank: Record<string, number> = { primary: 0, secondary: 1, info: 2 }
  suspectedDetails.sort((a, b) => {
    const ra = severityRank[a.finding?.severity ?? ''] ?? 3
    const rb = severityRank[b.finding?.severity ?? ''] ?? 3
    return ra - rb
  })

  return (
    <section className="topology-result">
      <h3>解析結果</h3>
      <div className="summary-grid">
        <div className="summary-card">
          <div className="summary-label">確信度</div>
          <div className="summary-value">{result.confidence.toFixed(2)}</div>
        </div>
        <div className="summary-card">
          <div className="summary-label">トークン (in/out)</div>
          <div className="summary-value">
            {result.metrics.tokens_in.toLocaleString()} / {result.metrics.tokens_out.toLocaleString()}
          </div>
        </div>
        <div className="summary-card">
          <div className="summary-label">レイテンシ</div>
          <div className="summary-value">{(result.metrics.latency_ms_total / 1000).toFixed(1)}s</div>
        </div>
        <div className="summary-card">
          <div className="summary-label">trace</div>
          <div className="summary-value mono small">
            {traceUrl ? (
              <a href={traceUrl} target="_blank" rel="noopener noreferrer">{result.trace_id.slice(0, 8)}… ↗</a>
            ) : result.trace_id.slice(0, 8) + '…'}
          </div>
        </div>
      </div>

      <h4>障害候補ノード（{suspectedDetails.length}）</h4>
      {suspectedDetails.length === 0 ? (
        <p className="muted">LLM はトポロジー上のノードを障害候補として特定しませんでした。</p>
      ) : (
        <ul className="suspected-detail-list">
          {suspectedDetails.map(({ node, finding }) => {
            const sev = finding?.severity || 'unknown'
            return (
              <li key={node.id} className={`suspected-detail sev-${sev}`}>
                <div className="suspected-detail-header">
                  <span className="suspected-id">{node.id}</span>
                  {node.type && <span className="suspected-type">[{node.type}]</span>}
                  {node.label && <span className="suspected-label">{node.label}</span>}
                  {sev !== 'unknown' && (
                    <span className={`severity-badge sev-${sev}`}>
                      {sev === 'primary' ? '直接原因' : sev === 'secondary' ? '影響を受けた側' : sev === 'info' ? '参考' : sev}
                    </span>
                  )}
                  {node.ip && <span className="suspected-ip">{node.ip}</span>}
                </div>
                <div className="suspected-detail-summary">
                  {finding?.summary
                    ? finding.summary
                    : <span className="muted">（LLM はこのノードを候補に挙げたが詳細な所見を返しませんでした）</span>}
                </div>
              </li>
            )
          })}
        </ul>
      )}

      <h4>根本原因候補（{result.root_cause_candidates.length}）</h4>
      <ul className="candidates candidates-grid">
        {result.root_cause_candidates.map((c, i) => (
          <li key={i}>
            <span className={`badge cat-${c.category}`}>{c.category}</span>
            <div className="summary-text">{c.summary}</div>
            {c.evidence.length > 0 && (
              <details>
                <summary>evidence ({c.evidence.length})</summary>
                <ul className="evidence-list">
                  {c.evidence.map((e, j) => <li key={j}><code>{e}</code></li>)}
                </ul>
              </details>
            )}
          </li>
        ))}
      </ul>

      <h4>推奨アクション（{result.recommended_actions.length}）</h4>
      <ul className="actions">
        {result.recommended_actions.map((a, i) => (
          <li key={i} className={a.human_judgment_required ? 'requires-human' : ''}>
            <span className={`risk risk-${a.risk_level}`}>{a.risk_level}</span>
            {a.human_judgment_required && <span className="hjr-badge">人間判断必須</span>}
            <span className="action-text">{a.action}</span>
          </li>
        ))}
      </ul>

      {/* 監査エージェント (Phase C) の所見 */}
      {result.audit_report && <AuditReportView report={result.audit_report} />}

      {/* 実際に動いたワークフロー: 委譲チェーン履歴 + 実行グラフ */}
      <h4>解析ワークフロー</h4>
      <DelegationHistoryView result={result} />
      {result.round_metrics.length > 0 && (
        <RoundMetricsView rounds={result.round_metrics} />
      )}
      {result.execution_graph_nodes.length > 0 && (
        <div className="topology-graph-wrap">
          <div className="topology-graph-caption">
            実際に呼ばれた監視ノードとモデル・トークン使用量
          </div>
          <GraphView
            nodes={result.execution_graph_nodes}
            edges={result.execution_graph_edges}
          />
        </div>
      )}
    </section>
  )
}
