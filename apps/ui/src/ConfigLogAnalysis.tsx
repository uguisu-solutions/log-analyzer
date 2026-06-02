/**
 * config-log 解析タブ。
 *
 * 構成図 + ノード別の Config / Log を入力に、rally (config4) で根本原因を解析する。
 * 解析モードを 2 軸で選べる:
 *
 *   1) 1 段階 (single): rally を 1 回だけ実行
 *        - config のみ  … ログ入力フォームを隠す
 *        - log のみ     … 設定入力フォームを隠す
 *        - config + log … 両方を同時に投入 (既定)
 *   2) 2 段階 (two_stage): Stage 1 で当たりをつけ、人間承認なしで自動的に Stage 2 で検証
 *        - config → log … コンフィグで仮説 → ログで検証
 *        - log → config … ログで仮説 → コンフィグで裏取り
 *
 * 既存のトポロジー解析タブ ([TopologyAnalysis.tsx](./TopologyAnalysis.tsx)) と
 * 同じ画像 + 矩形描画 + ノード別添付の UX を踏襲する。設計詳細は
 * [docs/plan/config_log_stages.md](../../docs/plan/config_log_stages.md)。
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
import { QuestionnairePanel } from './QuestionnairePanel'
import type {
  AnalysisResult,
  ConfigEntry,
  DelegationEvent,
  LogEntry,
  NodeAttachment,
  NodeAttachments,
  QuestionnaireAnswers,
  SSEEvent,
  StageOutput,
  SuspectedNodeFinding,
  TopologyDef,
  TopologyNode,
} from './types'

const API_BASE = 'http://localhost:8000'
// トポロジー解析タブとは別キーで保存 (誤共有を防ぐ)
const STORAGE_KEY = 'log-analyzer.config-log-topology-v1'
const MAX_IMAGE_BYTES = 5 * 1024 * 1024

// 解析モード
type AnalysisMode = 'single' | 'two_stage'
type SingleSource = 'config' | 'log' | 'both'
type StageOrder = 'config_log' | 'log_config'

interface Props {
  configList: ConfigEntry[]
  logs: LogEntry[]
  parseSSE: (response: Response) => AsyncGenerator<SSEEvent>
  renderEventSummary: (ev: SSEEvent) => React.ReactNode
  langfuseHost: string | null
}

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
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(t)) } catch {}
}
function uniqueNodeId(existing: TopologyNode[], base: string): string {
  if (!existing.some(n => n.id === base)) return base
  for (let i = 2; i < 1000; i++) {
    const c = `${base}-${i}`
    if (!existing.some(n => n.id === c)) return c
  }
  return `${base}-${Date.now()}`
}
function toNormalized(px: number, py: number, rect: DOMRect): { x: number; y: number } {
  return {
    x: Math.max(0, Math.min(1, (px - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (py - rect.top) / rect.height)),
  }
}

// 進行中の Stage ステータス
type StageStatus =
  | 'idle'
  | 'single_running'
  | 'stage1_running'
  | 'stage2_running'
  | 'completed'
  | 'aborted'
  | 'error'

export function ConfigLogAnalysis({ configList, logs, parseSSE, renderEventSummary, langfuseHost }: Props) {
  // ─── トポロジー (永続化) ──────────────────────────────────────
  const [topology, setTopology] = useState<TopologyDef>(() => loadTopology())
  useEffect(() => { saveTopology(topology) }, [topology])

  // ─── ノード別添付 (揮発) ─────────────────────────────────────
  const [nodeLogs, setNodeLogs] = useState<NodeAttachments>({})
  const [nodeConfigs, setNodeConfigs] = useState<NodeAttachments>({})

  // ─── 編集モード ──────────────────────────────────────────────
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [editMode, setEditMode] = useState<'select' | 'add'>('select')
  const [drawing, setDrawing] = useState<{ startX: number; startY: number; curX: number; curY: number } | null>(null)
  // 構成図画像のファイル D&D 用
  const [canvasDragOver, setCanvasDragOver] = useState(false)

  // ─── 実行構成 ────────────────────────────────────────────────
  const rallyConfigs = useMemo(() => configList.filter(isRallyConfig), [configList])
  const [selectedConfig, setSelectedConfig] = useState<string>('')
  useEffect(() => {
    if (!selectedConfig && rallyConfigs.length > 0) setSelectedConfig(rallyConfigs[0].id)
  }, [rallyConfigs, selectedConfig])
  const [rallyMaxRounds, setRallyMaxRounds] = useState<number>(3)
  // 解析モード (既定: 1 段階 config + log 同時)
  const [analysisMode, setAnalysisMode] = useState<AnalysisMode>('single')
  const [singleSource, setSingleSource] = useState<SingleSource>('both')
  const [stageOrder, setStageOrder] = useState<StageOrder>('config_log')
  // 問診票回答 (Phase B、揮発)
  const [questionnaireAnswers, setQuestionnaireAnswers] = useState<QuestionnaireAnswers>({})
  // 監査エージェント (Phase C): integrator 後に GPT で独立検証
  const [auditAfterIntegrator, setAuditAfterIntegrator] = useState<boolean>(false)
  // 表示モード (Phase E): デフォルトをチャットに (議事録の UI 要求)
  const [viewMode, setViewMode] = useState<'standard' | 'chat'>('chat')

  // ─── 実行状態 ────────────────────────────────────────────────
  const [stageStatus, setStageStatus] = useState<StageStatus>('idle')
  const [streamEvents, setStreamEvents] = useState<SSEEvent[]>([])
  const [runId, setRunId] = useState<string | null>(null)
  const [stageOneOutput, setStageOneOutput] = useState<StageOutput | null>(null)
  const [stageTwoOutput, setStageTwoOutput] = useState<StageOutput | null>(null)
  const [finalResult, setFinalResult] = useState<AnalysisResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [decisionBusy, setDecisionBusy] = useState(false)
  // rally_max_rounds 到達時の継続/停止モーダル (Stage 内部で発火しうる)
  const [pendingConfirmation, setPendingConfirmation] = useState<{
    round: number
    rally_max_rounds: number
    delegation_history: DelegationEvent[]
  } | null>(null)
  // 結果ペインで表示中の Stage タブ
  const [resultTab, setResultTab] = useState<'combined' | 'stage1' | 'stage2'>('combined')

  const abortRef = useRef<AbortController | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const imageRef = useRef<HTMLImageElement | null>(null)

  // ─── モードから導出するフラグ ────────────────────────────────
  const isTwoStage = analysisMode === 'two_stage'
  // 入力フォームの表示制御:
  //   1 段階 config のみ → ログ入力フォーム非表示
  //   1 段階 log のみ    → 設定入力フォーム非表示
  //   それ以外 (both / 2 段階) → 両方表示
  const showConfigForm = !(analysisMode === 'single' && singleSource === 'log')
  const showLogForm = !(analysisMode === 'single' && singleSource === 'config')
  const isRunning =
    stageStatus === 'single_running' || stageStatus === 'stage1_running' ||
    stageStatus === 'stage2_running'
  // 2 段階の Stage 1/2 のデータ種別 (live ラベルや結果表示に使う)
  const stageKinds = useMemo<[string, string]>(
    () => (stageOrder === 'config_log' ? ['config', 'log'] : ['log', 'config']),
    [stageOrder],
  )

  // 「現在ハイライト対象」: 実行中の stage に応じて切り替える
  const currentHighlightFindings = useMemo<SuspectedNodeFinding[]>(() => {
    if (stageStatus === 'completed' || stageStatus === 'aborted') {
      if (resultTab === 'stage1') return stageOneOutput?.suspected_node_findings ?? []
      if (resultTab === 'stage2') return stageTwoOutput?.suspected_node_findings ?? []
      return finalResult?.suspected_node_findings ?? []
    }
    if (stageStatus === 'stage2_running') {
      return stageOneOutput?.suspected_node_findings ?? []
    }
    return []
  }, [stageStatus, resultTab, stageOneOutput, stageTwoOutput, finalResult])

  const suspectedSet = useMemo(
    () => new Set(currentHighlightFindings.map(f => f.node_id)),
    [currentHighlightFindings],
  )
  const severityById = useMemo(() => {
    const m = new Map<string, string>()
    for (const f of currentHighlightFindings) m.set(f.node_id, f.severity || '')
    return m
  }, [currentHighlightFindings])
  const highlightClass = useCallback((nodeId: string): string => {
    if (!suspectedSet.has(nodeId)) return ''
    const sev = severityById.get(nodeId) ?? ''
    if (sev === 'info') return ''
    if (sev === 'secondary') return 'is-suspected sev-secondary'
    return 'is-suspected sev-primary'
  }, [suspectedSet, severityById])

  // ─── 画像 / ノード操作 (トポロジー解析タブ同等) ──────────────
  const handleImageFile = useCallback((file: File) => {
    if (file.size > MAX_IMAGE_BYTES) {
      setError(`画像は ${MAX_IMAGE_BYTES / 1024 / 1024}MB 以下にしてください`)
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = String(reader.result)
      const img = new Image()
      img.onload = () => {
        setTopology(t => ({ ...t, image: dataUrl, imageWidth: img.naturalWidth, imageHeight: img.naturalHeight }))
        setError(null)
      }
      img.onerror = () => setError('画像のデコードに失敗しました')
      img.src = dataUrl
    }
    reader.onerror = () => setError('画像の読み込みに失敗しました')
    reader.readAsDataURL(file)
  }, [])
  const onImageInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]; if (f) handleImageFile(f); e.target.value = ''
  }
  // ─── ファイル D&D ────────────────────────────────────────────
  const onCanvasDragOver = (e: React.DragEvent) => {
    if (Array.from(e.dataTransfer.types).includes('Files')) { e.preventDefault(); setCanvasDragOver(true) }
  }
  const onCanvasDragLeave = (e: React.DragEvent) => {
    // 子要素へ移動しただけの dragleave は無視
    if (e.currentTarget === e.target) setCanvasDragOver(false)
  }
  const onCanvasDrop = (e: React.DragEvent) => {
    if (!Array.from(e.dataTransfer.types).includes('Files')) return
    e.preventDefault(); setCanvasDragOver(false)
    const img = Array.from(e.dataTransfer.files).find(f => f.type.startsWith('image/'))
    if (img) handleImageFile(img)
    else setError('構成図には画像ファイル (PNG / JPEG / SVG) をドロップしてください')
  }
  // ログ / config をファイルごと (複数可) ノードに追加
  const addFilesToNode = (setter: Dispatch<SetStateAction<NodeAttachments>>, nodeId: string, files: FileList) => {
    for (const file of Array.from(files)) {
      const reader = new FileReader()
      reader.onload = () => addAttachment(setter, nodeId, { name: file.name, content: String(reader.result ?? '') })
      reader.onerror = () => setError(`ファイル読み込みに失敗: ${file.name}`)
      reader.readAsText(file)
    }
  }
  const onCanvasMouseDown = (e: React.MouseEvent) => {
    if (editMode !== 'add' || !topology.image) return
    const rect = containerRef.current?.getBoundingClientRect(); if (!rect) return
    const { x, y } = toNormalized(e.clientX, e.clientY, rect)
    setDrawing({ startX: x, startY: y, curX: x, curY: y })
  }
  const onCanvasMouseMove = (e: React.MouseEvent) => {
    if (!drawing) return
    const rect = containerRef.current?.getBoundingClientRect(); if (!rect) return
    const { x, y } = toNormalized(e.clientX, e.clientY, rect)
    setDrawing(d => d ? { ...d, curX: x, curY: y } : null)
  }
  const onCanvasMouseUp = () => {
    if (!drawing) return
    const x = Math.min(drawing.startX, drawing.curX), y = Math.min(drawing.startY, drawing.curY)
    const w = Math.abs(drawing.curX - drawing.startX), h = Math.abs(drawing.curY - drawing.startY)
    setDrawing(null)
    if (w < 0.01 || h < 0.01) return
    const newId = uniqueNodeId(topology.nodes, 'node')
    const newNode: TopologyNode = { id: newId, type: '', label: '', ip: '', x, y, w, h }
    setTopology(t => ({ ...t, nodes: [...t.nodes, newNode] }))
    setEditMode('select'); setSelectedNodeId(newId)
  }
  const updateNode = (id: string, patch: Partial<TopologyNode>) => {
    setTopology(t => ({ ...t, nodes: t.nodes.map(n => n.id === id ? { ...n, ...patch } : n) }))
  }
  const renameNode = (oldId: string, newId: string) => {
    const trimmed = newId.trim(); if (!trimmed || trimmed === oldId) return
    setTopology(t => {
      if (t.nodes.some(n => n.id === trimmed)) return t
      return {
        ...t,
        nodes: t.nodes.map(n => n.id === oldId ? { ...n, id: trimmed } : n),
        links: t.links.map(l => ({ source: l.source === oldId ? trimmed : l.source, target: l.target === oldId ? trimmed : l.target })),
      }
    })
    const renameKey = (m: NodeAttachments): NodeAttachments => {
      if (!(oldId in m)) return m
      const next: NodeAttachments = {}
      for (const [k, val] of Object.entries(m)) next[k === oldId ? trimmed : k] = val
      return next
    }
    setNodeLogs(renameKey); setNodeConfigs(renameKey)
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
      for (const [k, val] of Object.entries(m)) if (k !== id) next[k] = val
      return next
    }
    setNodeLogs(dropKey); setNodeConfigs(dropKey)
    if (selectedNodeId === id) setSelectedNodeId(null)
  }
  const clearAll = () => {
    if (!confirm('構成図とノード定義をすべて削除します。よろしいですか？')) return
    setTopology(EMPTY_TOPOLOGY); setNodeLogs({}); setNodeConfigs({})
    setSelectedNodeId(null); setStageOneOutput(null); setStageTwoOutput(null)
    setFinalResult(null); setStreamEvents([]); setStageStatus('idle')
  }
  const addAttachment = (setter: Dispatch<SetStateAction<NodeAttachments>>, nodeId: string, initial: NodeAttachment) => {
    setter(prev => ({ ...prev, [nodeId]: [...(prev[nodeId] ?? []), initial] }))
  }
  const updateAttachment = (setter: Dispatch<SetStateAction<NodeAttachments>>, nodeId: string, index: number, patch: Partial<NodeAttachment>) => {
    setter(prev => {
      const list = prev[nodeId] ?? []
      if (index < 0 || index >= list.length) return prev
      const next = [...list]; next[index] = { ...next[index], ...patch }
      return { ...prev, [nodeId]: next }
    })
  }
  const removeAttachment = (setter: Dispatch<SetStateAction<NodeAttachments>>, nodeId: string, index: number) => {
    setter(prev => {
      const list = prev[nodeId] ?? []; if (index < 0 || index >= list.length) return prev
      const next = list.filter((_, i) => i !== index)
      if (next.length === 0) {
        const { [nodeId]: _drop, ...rest } = prev; return rest
      }
      return { ...prev, [nodeId]: next }
    })
  }
  const loadSampleIntoNode = async (id: string, sampleName: string) => {
    try {
      const r = await fetch(`${API_BASE}/api/logs/${encodeURIComponent(sampleName)}/content`)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const data = (await r.json()) as { content: string }
      addAttachment(setNodeLogs, id, { name: sampleName, content: data.content })
    } catch (e) {
      setError(`サンプルログの読み込みに失敗: ${(e as Error).message}`)
    }
  }

  // ─── 実行可否 ────────────────────────────────────────────────
  const filteredAttachments = useCallback((m: NodeAttachments): NodeAttachments => {
    const out: NodeAttachments = {}
    for (const [nid, list] of Object.entries(m)) {
      const kept = list.filter(a => a.content.trim().length > 0)
      if (kept.length > 0) out[nid] = kept
    }
    return out
  }, [])
  const hasConfig = useMemo(
    () => Object.values(nodeConfigs).some(list => list.some(a => a.content.trim().length > 0)),
    [nodeConfigs],
  )
  const hasLog = useMemo(
    () => Object.values(nodeLogs).some(list => list.some(a => a.content.trim().length > 0)),
    [nodeLogs],
  )
  const canRun = useMemo(() => {
    if (isRunning) return false
    if (!selectedConfig) return false
    if (topology.nodes.length === 0) return false
    if (analysisMode === 'single') {
      if (singleSource === 'config') return hasConfig
      if (singleSource === 'log') return hasLog
      return hasConfig || hasLog
    }
    // 2 段階: Stage 1 の始動データ種別が揃っていること
    return stageKinds[0] === 'config' ? hasConfig : hasLog
  }, [isRunning, selectedConfig, topology.nodes, analysisMode, singleSource, stageKinds, hasConfig, hasLog])

  // ─── 実行 ────────────────────────────────────────────────────
  const run = async () => {
    if (!canRun) return
    setError(null)
    setStreamEvents([])
    setStageOneOutput(null); setStageTwoOutput(null); setFinalResult(null)
    setStageStatus(isTwoStage ? 'stage1_running' : 'single_running')
    setResultTab('combined')
    const ctrl = new AbortController(); abortRef.current = ctrl
    try {
      const body = {
        config: selectedConfig,
        rally_max_rounds: rallyMaxRounds,
        topology: {
          nodes: topology.nodes.map(n => ({ id: n.id, type: n.type, label: n.label, ip: n.ip })),
          links: topology.links,
        },
        // フォーム非表示の種別は送らない (意図に忠実 + 無駄なトークン削減)
        node_logs: showLogForm ? filteredAttachments(nodeLogs) : {},
        node_configs: showConfigForm ? filteredAttachments(nodeConfigs) : {},
        analysis_mode: analysisMode,
        single_source: singleSource,
        stage_order: stageOrder,
        questionnaire_answers: questionnaireAnswers,
        audit_after_integrator: auditAfterIntegrator,
      }
      const r = await fetch(`${API_BASE}/api/runs/config-log-stream`, {
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
        } else if (ev.kind === 'single_stage_start') {
          setStageStatus('single_running')
        } else if (ev.kind === 'stage_one_complete') {
          // 人間承認は廃止。Stage 1 結果を取り込み、バックエンドの自動進行に任せる
          // (最終結果には stage_outputs[0] として Stage 1 が残る)。
          const so = ev.data.stage_output as StageOutput | undefined
          if (so) setStageOneOutput(so)
        } else if (ev.kind === 'stage_two_start') {
          setStageStatus('stage2_running')
        } else if (ev.kind === 'await_confirmation') {
          // Stage 内部で rally_max_rounds 到達 → 継続/停止モーダル
          setPendingConfirmation({
            round: Number(ev.data.round ?? 0),
            rally_max_rounds: Number(ev.data.rally_max_rounds ?? 0),
            delegation_history: (ev.data.delegation_history as DelegationEvent[]) ?? [],
          })
        } else if (ev.kind === 'user_decision') {
          const action = String(ev.data.action ?? '')
          if (action === 'continue' || action === 'stop') {
            setPendingConfirmation(null)
          }
        } else if (ev.kind === 'final') {
          const res = ev.data.result as AnalysisResult | undefined
          if (res) {
            setFinalResult(res)
            // stage_outputs は配列順に Stage 1 / Stage 2 (順序非依存で位置参照)
            const s1 = res.stage_outputs[0] ?? null
            const s2 = res.stage_outputs.length > 1 ? res.stage_outputs[1] : null
            if (s1) setStageOneOutput(s1)
            if (s2) setStageTwoOutput(s2)
            // 2 段階で Stage 2 が無い = abort、それ以外は完了
            setStageStatus(isTwoStage && !s2 ? 'aborted' : 'completed')
          }
        } else if (ev.kind === 'error') {
          setError(String(ev.data.message ?? 'unknown stream error'))
          setStageStatus('error')
        }
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        setError((e as Error).message)
        setStageStatus('error')
      }
    } finally {
      abortRef.current = null
    }
  }
  const cancel = () => { abortRef.current?.abort() }

  // ─── decision API 呼び出し ────────────────────────────────────
  // continue/stop: rally_max_rounds 到達時の継続/停止用 (Stage 内部から発火)
  // ※ Stage 1→2 の人間承認 (advance/abort) は廃止し、バックエンドが自動進行する。
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

  const selectedNode = topology.nodes.find(n => n.id === selectedNodeId) ?? null

  return (
    <section className="topology-mode config-log-mode">
      <div className="topology-header">
        <h2>config-log 解析</h2>
        <p className="muted">
          構成図と Config / Log を入力に rally で根本原因を解析します。1 段階（config のみ / log のみ /
          config + log 同時）と 2 段階（config → log / log → config）を選べます。
        </p>
      </div>

      <StageIndicator status={stageStatus} analysisMode={analysisMode} stageKinds={stageKinds} singleSource={singleSource} />

      <div className="topology-toolbar">
        <label className="btn-file">
          画像を選択
          <input type="file" accept="image/png,image/jpeg,image/svg+xml" onChange={onImageInputChange} hidden />
        </label>
        <div className="toolbar-modes">
          <button className={editMode === 'select' ? 'tab active' : 'tab'} onClick={() => setEditMode('select')} disabled={!topology.image}>選択・編集</button>
          <button className={editMode === 'add' ? 'tab active' : 'tab'} onClick={() => setEditMode('add')} disabled={!topology.image}>ノード追加（ドラッグで矩形描画）</button>
        </div>
        <button className="btn-secondary" onClick={clearAll} disabled={!topology.image && topology.nodes.length === 0}>すべてクリア</button>
      </div>

      <div className="topology-canvas-row">
        <div
          ref={containerRef}
          className={`topology-canvas mode-${editMode}${canvasDragOver ? ' drag-over' : ''}`}
          onMouseDown={onCanvasMouseDown} onMouseMove={onCanvasMouseMove}
          onMouseUp={onCanvasMouseUp} onMouseLeave={() => setDrawing(null)}
          onDragOver={onCanvasDragOver} onDragLeave={onCanvasDragLeave} onDrop={onCanvasDrop}
        >
          {topology.image ? (
            <>
              <img ref={imageRef} src={topology.image} alt="topology" className="topology-image" draggable={false} />
              <svg className="topology-overlay" viewBox="0 0 1 1" preserveAspectRatio="none">
                {topology.nodes.map(n => {
                  const isSelected = n.id === selectedNodeId
                  const hl = highlightClass(n.id)
                  return (
                    <g key={n.id}>
                      <rect
                        x={n.x} y={n.y} width={n.w} height={n.h}
                        className={['node-rect', isSelected ? 'is-selected' : '', hl].filter(Boolean).join(' ')}
                        onMouseDown={e => { if (editMode === 'select') { e.stopPropagation(); setSelectedNodeId(n.id) } }}
                        vectorEffect="non-scaling-stroke"
                      />
                      <text x={n.x + 0.005} y={n.y + 0.018} className={['node-label', hl].filter(Boolean).join(' ')}>
                        {n.id}{n.type ? ` [${n.type}]` : ''}
                      </text>
                    </g>
                  )
                })}
                {drawing && (
                  <rect
                    x={Math.min(drawing.startX, drawing.curX)} y={Math.min(drawing.startY, drawing.curY)}
                    width={Math.abs(drawing.curX - drawing.startX)} height={Math.abs(drawing.curY - drawing.startY)}
                    className="node-rect drawing" vectorEffect="non-scaling-stroke"
                  />
                )}
              </svg>
            </>
          ) : (
            <div className="topology-empty">
              画像を選択、またはここに構成図ファイルをドラッグ＆ドロップ<br />
              （PNG / JPEG / SVG, 最大 5MB）
            </div>
          )}
        </div>
        <aside className="topology-sidebar">
          {selectedNode ? (
            <CfNodeEditor
              node={selectedNode}
              logs={nodeLogs[selectedNode.id] ?? []}
              configs={nodeConfigs[selectedNode.id] ?? []}
              sampleLogs={logs}
              showConfigForm={showConfigForm}
              showLogForm={showLogForm}
              onUpdate={(patch) => updateNode(selectedNode.id, patch)}
              onRename={(newId) => renameNode(selectedNode.id, newId)}
              onAddLog={() => addAttachment(setNodeLogs, selectedNode.id, { name: '', content: '' })}
              onUpdateLog={(i, patch) => updateAttachment(setNodeLogs, selectedNode.id, i, patch)}
              onRemoveLog={(i) => removeAttachment(setNodeLogs, selectedNode.id, i)}
              onAddConfig={() => addAttachment(setNodeConfigs, selectedNode.id, { name: '', content: '' })}
              onUpdateConfig={(i, patch) => updateAttachment(setNodeConfigs, selectedNode.id, i, patch)}
              onRemoveConfig={(i) => removeAttachment(setNodeConfigs, selectedNode.id, i)}
              onDropLogFiles={(files) => addFilesToNode(setNodeLogs, selectedNode.id, files)}
              onDropConfigFiles={(files) => addFilesToNode(setNodeConfigs, selectedNode.id, files)}
              onLoadSample={(name) => loadSampleIntoNode(selectedNode.id, name)}
              onDelete={() => deleteNode(selectedNode.id)}
              isSuspected={suspectedSet.has(selectedNode.id)}
              allNodes={topology.nodes}
            />
          ) : (
            <div className="sidebar-hint">
              {editMode === 'add' ? '画像上でドラッグしてノード矩形を描画' : 'ノード矩形をクリックして編集'}
              <CfNodeList nodes={topology.nodes} nodeLogs={nodeLogs} nodeConfigs={nodeConfigs} severityById={severityById}
                showConfigForm={showConfigForm} showLogForm={showLogForm} onSelect={setSelectedNodeId} />
            </div>
          )}
        </aside>
      </div>

      {/* standard モード時のみ実行バー直前に表示 (chat モードはチャット内に統合) */}
      {viewMode === 'standard' && (
        <QuestionnairePanel
          answers={questionnaireAnswers}
          onAnswersChange={setQuestionnaireAnswers}
          disabled={isRunning}
        />
      )}

      <ModeSelector
        analysisMode={analysisMode} singleSource={singleSource} stageOrder={stageOrder}
        disabled={isRunning}
        onAnalysisMode={setAnalysisMode}
        onSingleSource={setSingleSource}
        onStageOrder={setStageOrder}
      />

      <div className="topology-run-bar">
        <label>
          rally_max_rounds {isTwoStage ? '(Stage 毎)' : ''}:
          <input type="number" min={1} max={20} value={rallyMaxRounds}
            onChange={e => setRallyMaxRounds(Math.max(1, Math.min(20, Number(e.target.value) || 3)))}
            disabled={isRunning} />
        </label>
        <label className="audit-toggle">
          <input type="checkbox" checked={auditAfterIntegrator}
            onChange={e => setAuditAfterIntegrator(e.target.checked)}
            disabled={isRunning} />
          <span>GPT 監査も実行</span>
        </label>
        <button onClick={run} disabled={!canRun} className="run-button">
          {stageStatus === 'single_running' ? '解析中…'
            : stageStatus === 'stage1_running' ? 'Stage 1 実行中…'
            : stageStatus === 'stage2_running' ? 'Stage 2 実行中…'
            : '解析を開始'}
        </button>
        {isRunning && (
          <button onClick={cancel} className="btn-secondary">中止</button>
        )}
      </div>

      {error && <div className="topology-error">エラー: {error}</div>}

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
            disabled={isRunning}
          />
          {streamEvents.length > 0 ? (
            <LiveChatView events={streamEvents} questionnaireAnswers={questionnaireAnswers} />
          ) : (
            <div className="live-chat-empty muted">実行を開始するとここに会話が表示されます。</div>
          )}
          <ChatInput
            runId={runId}
            disabled={stageStatus !== 'single_running' && stageStatus !== 'stage1_running' && stageStatus !== 'stage2_running'}
          />
        </section>
      )}

      {/* 標準モード: 従来のイベントリスト */}
      {viewMode === 'standard' && streamEvents.length > 0 && (
        <section className="realtime-stream">
          <h3>
            リアルタイム実行ログ
            <span className="realtime-count">{streamEvents.length} イベント</span>
          </h3>
          <ol className="stream-events">
            {streamEvents.map((ev, i) => {
              const ord = (ev.data as { stage_ordinal?: number }).stage_ordinal
              const stage = (ev.data as { stage?: string }).stage
              return (
                <li key={i} className={`stream-event kind-${ev.kind} ${stage ? `stg-${stage}` : ''}`}>
                  {ord && <span className={`stage-tag stage-${stage}`}>Stage {ord}</span>}
                  <span className="stream-kind">{ev.kind}</span>
                  <span className="stream-body">{renderEventSummary(ev)}</span>
                </li>
              )
            })}
          </ol>
        </section>
      )}

      {/* rally_max_rounds 到達時の継続/停止モーダル (Stage 内部から発火) */}
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

      {/* 最終結果ペイン */}
      {(stageStatus === 'completed' || stageStatus === 'aborted') && finalResult && (
        <section className="topology-result">
          <h3>解析結果</h3>
          {viewMode === 'chat' ? (
            <ChatHistoryView result={finalResult} questionnaireAnswers={questionnaireAnswers} />
          ) : (
            <>
              <ResultTabs
                current={resultTab}
                onChange={setResultTab}
                isTwoStage={isTwoStage}
                stageOneOutput={stageOneOutput}
                stageTwoOutput={stageTwoOutput}
              />
              {resultTab === 'combined' && (
                <CombinedResultView result={finalResult} isTwoStage={isTwoStage} stageOneOutput={stageOneOutput} stageTwoOutput={stageTwoOutput} topology={topology} langfuseHost={langfuseHost} />
              )}
              {resultTab === 'stage1' && stageOneOutput && (
                <StageResultView stage={stageOneOutput} topology={topology} />
              )}
              {resultTab === 'stage2' && stageTwoOutput && (
                <StageResultView stage={stageTwoOutput} topology={topology} />
              )}
            </>
          )}
        </section>
      )}
    </section>
  )
}

// ─── サブコンポーネント ──────────────────────────────────────

interface ModeSelectorProps {
  analysisMode: AnalysisMode
  singleSource: SingleSource
  stageOrder: StageOrder
  disabled: boolean
  onAnalysisMode: (m: AnalysisMode) => void
  onSingleSource: (s: SingleSource) => void
  onStageOrder: (o: StageOrder) => void
}

function ModeSelector({ analysisMode, singleSource, stageOrder, disabled, onAnalysisMode, onSingleSource, onStageOrder }: ModeSelectorProps) {
  return (
    <div className="topology-mode-bar">
      <div className="mode-toggle">
        <span className="mode-toggle-label">解析段階:</span>
        <label className="radio-pill">
          <input type="radio" name="cl-stage" checked={analysisMode === 'single'}
            onChange={() => onAnalysisMode('single')} disabled={disabled} />
          <span>1 段階</span>
        </label>
        <label className="radio-pill">
          <input type="radio" name="cl-stage" checked={analysisMode === 'two_stage'}
            onChange={() => onAnalysisMode('two_stage')} disabled={disabled} />
          <span>2 段階（自動進行）</span>
        </label>
      </div>

      {analysisMode === 'single' ? (
        <div className="mode-toggle">
          <span className="mode-toggle-label">使用データ:</span>
          <label className="radio-pill">
            <input type="radio" name="cl-source" checked={singleSource === 'config'}
              onChange={() => onSingleSource('config')} disabled={disabled} />
            <span>config のみ</span>
          </label>
          <label className="radio-pill">
            <input type="radio" name="cl-source" checked={singleSource === 'log'}
              onChange={() => onSingleSource('log')} disabled={disabled} />
            <span>log のみ</span>
          </label>
          <label className="radio-pill">
            <input type="radio" name="cl-source" checked={singleSource === 'both'}
              onChange={() => onSingleSource('both')} disabled={disabled} />
            <span>config + log 同時</span>
          </label>
        </div>
      ) : (
        <div className="mode-toggle">
          <span className="mode-toggle-label">順序:</span>
          <label className="radio-pill">
            <input type="radio" name="cl-order" checked={stageOrder === 'config_log'}
              onChange={() => onStageOrder('config_log')} disabled={disabled} />
            <span>config → log</span>
          </label>
          <label className="radio-pill">
            <input type="radio" name="cl-order" checked={stageOrder === 'log_config'}
              onChange={() => onStageOrder('log_config')} disabled={disabled} />
            <span>log → config</span>
          </label>
        </div>
      )}

      <p className="mode-toggle-hint muted">
        {analysisMode === 'single'
          ? (singleSource === 'config'
              ? 'config のみで rally を 1 回実行します（ログ入力欄は非表示）。'
              : singleSource === 'log'
              ? 'log のみで rally を 1 回実行します（設定入力欄は非表示）。'
              : 'config と log を同時に投入し rally を 1 回実行します。')
          : (stageOrder === 'config_log'
              ? 'コンフィグで当たりをつけ、そのまま自動でログ検証へ進む 2 段階です（人間承認なし）。'
              : 'ログで当たりをつけ、そのまま自動でコンフィグ裏取りへ進む 2 段階です（人間承認なし）。')}
      </p>
    </div>
  )
}

interface StageIndicatorProps {
  status: StageStatus
  analysisMode: AnalysisMode
  stageKinds: [string, string]
  singleSource: SingleSource
}

function kindLabel(kind: string): string {
  if (kind === 'config') return 'コンフィグ'
  if (kind === 'log') return 'ログ'
  if (kind === 'both') return 'コンフィグ + ログ'
  return kind
}

function StageIndicator({ status, analysisMode, stageKinds, singleSource }: StageIndicatorProps) {
  if (analysisMode === 'single') {
    const running = status === 'single_running'
    const done = status === 'completed'
    return (
      <div className="stage-indicator">
        <div className={['stage-step', running ? 'active' : '', done ? 'done' : ''].filter(Boolean).join(' ')}>
          <span className="stage-num">1</span>
          <span className="stage-name">{kindLabel(singleSource)}解析</span>
        </div>
        <div className="stage-arrow">→</div>
        <div className={['stage-step', done ? 'done' : ''].filter(Boolean).join(' ')}>
          <span className="stage-num">★</span>
          <span className="stage-name">完了</span>
        </div>
      </div>
    )
  }
  // 2 段階 (人間承認は廃止 = 自動進行)
  const stage1Done = status === 'stage2_running' || status === 'completed' || status === 'aborted'
  const stage2Done = status === 'completed'
  const stage1Active = status === 'stage1_running'
  const stage2Active = status === 'stage2_running'
  return (
    <div className="stage-indicator">
      <div className={['stage-step', stage1Active ? 'active' : '', stage1Done ? 'done' : ''].filter(Boolean).join(' ')}>
        <span className="stage-num">1</span>
        <span className="stage-name">{kindLabel(stageKinds[0])}解析</span>
      </div>
      <div className="stage-arrow">→</div>
      <div className={['stage-step', stage2Active ? 'active' : '', stage2Done ? 'done' : '', status === 'aborted' ? 'skipped' : ''].filter(Boolean).join(' ')}>
        <span className="stage-num">2</span>
        <span className="stage-name">{kindLabel(stageKinds[1])}検証</span>
      </div>
      <div className="stage-arrow">→</div>
      <div className={['stage-step', status === 'completed' || status === 'aborted' ? 'done' : ''].filter(Boolean).join(' ')}>
        <span className="stage-num">★</span>
        <span className="stage-name">完了</span>
      </div>
    </div>
  )
}

interface ResultTabsProps {
  current: 'combined' | 'stage1' | 'stage2'
  onChange: (t: 'combined' | 'stage1' | 'stage2') => void
  isTwoStage: boolean
  stageOneOutput: StageOutput | null
  stageTwoOutput: StageOutput | null
}

function ResultTabs({ current, onChange, isTwoStage, stageOneOutput, stageTwoOutput }: ResultTabsProps) {
  // 1 段階モードでは Stage タブを出さない (統合のみ)
  if (!isTwoStage) return null
  return (
    <div className="result-tabs">
      <button className={current === 'combined' ? 'tab active' : 'tab'} onClick={() => onChange('combined')}>統合</button>
      <button className={current === 'stage1' ? 'tab active' : 'tab'} onClick={() => onChange('stage1')} disabled={!stageOneOutput}>
        {stageOneOutput?.stage_label || 'Stage 1'}
      </button>
      <button className={current === 'stage2' ? 'tab active' : 'tab'} onClick={() => onChange('stage2')} disabled={!stageTwoOutput}>
        {stageTwoOutput?.stage_label || 'Stage 2'} {stageTwoOutput ? '' : '— 未実行'}
      </button>
    </div>
  )
}

interface CombinedResultViewProps {
  result: AnalysisResult
  isTwoStage: boolean
  stageOneOutput: StageOutput | null
  stageTwoOutput: StageOutput | null
  topology: TopologyDef
  langfuseHost: string | null
}

function CombinedResultView({ result, isTwoStage, stageOneOutput, stageTwoOutput, topology, langfuseHost }: CombinedResultViewProps) {
  const traceUrl = langfuseHost ? `${langfuseHost}/trace/${result.trace_id}` : null
  return (
    <>
      <div className="summary-grid">
        <div className="summary-card">
          <div className="summary-label">最終確信度</div>
          <div className="summary-value">{result.confidence.toFixed(2)}</div>
        </div>
        <div className="summary-card">
          <div className="summary-label">トークン合計 (in/out)</div>
          <div className="summary-value">{result.metrics.tokens_in.toLocaleString()} / {result.metrics.tokens_out.toLocaleString()}</div>
        </div>
        <div className="summary-card">
          <div className="summary-label">合計レイテンシ</div>
          <div className="summary-value">{(result.metrics.latency_ms_total / 1000).toFixed(1)}s</div>
        </div>
        <div className="summary-card">
          <div className="summary-label">trace</div>
          <div className="summary-value mono small">
            {traceUrl ? <a href={traceUrl} target="_blank" rel="noopener noreferrer">{result.trace_id.slice(0, 8)}… ↗</a> : result.trace_id.slice(0, 8) + '…'}
          </div>
        </div>
      </div>

      {/* 2 段階モードのときだけ Stage 別サマリを出す */}
      {isTwoStage && (
        <>
          <h4>Stage 別サマリ</h4>
          <div className="stage-summary-grid">
            {stageOneOutput && (
              <div className="stage-summary-card stage-config">
                <div className="stage-summary-head">{stageOneOutput.stage_label || 'Stage 1'}</div>
                <div>確信度: <strong>{stageOneOutput.confidence.toFixed(2)}</strong></div>
                <div>候補ノード: {stageOneOutput.suspected_node_ids.join(', ') || '(なし)'}</div>
                <div className="stage-summary-text">{stageOneOutput.summary || '(要約なし)'}</div>
                <div className="muted small">tokens {stageOneOutput.tokens_in.toLocaleString()} / {stageOneOutput.tokens_out.toLocaleString()} · {(stageOneOutput.latency_ms_total / 1000).toFixed(1)}s · {stageOneOutput.delegation_rounds} rounds</div>
              </div>
            )}
            {stageTwoOutput ? (
              <div className="stage-summary-card stage-log">
                <div className="stage-summary-head">{stageTwoOutput.stage_label || 'Stage 2'}</div>
                <div>確信度: <strong>{stageTwoOutput.confidence.toFixed(2)}</strong></div>
                <div>候補ノード: {stageTwoOutput.suspected_node_ids.join(', ') || '(なし)'}</div>
                <div className="stage-summary-text">{stageTwoOutput.summary || '(要約なし)'}</div>
                <div className="muted small">tokens {stageTwoOutput.tokens_in.toLocaleString()} / {stageTwoOutput.tokens_out.toLocaleString()} · {(stageTwoOutput.latency_ms_total / 1000).toFixed(1)}s · {stageTwoOutput.delegation_rounds} rounds</div>
              </div>
            ) : (
              <div className="stage-summary-card stage-log muted">
                Stage 2 の結果がありません。
              </div>
            )}
          </div>
        </>
      )}

      <h4>最終 障害候補ノード（{result.suspected_node_findings.length}）</h4>
      <ul className="suspected-detail-list">
        {topology.nodes.filter(n => result.suspected_node_ids.includes(n.id)).map(n => {
          const f = result.suspected_node_findings.find(x => x.node_id === n.id)
          return (
            <li key={n.id} className={`suspected-detail sev-${f?.severity || 'unknown'}`}>
              <div className="suspected-detail-header">
                <span className="suspected-id">{n.id}</span>
                {n.type && <span className="suspected-type">[{n.type}]</span>}
                {f?.severity && <span className={`severity-badge sev-${f.severity}`}>{f.severity === 'primary' ? '直接原因' : f.severity === 'secondary' ? '影響を受けた側' : '参考'}</span>}
              </div>
              <div className="suspected-detail-summary">{f?.summary || '(詳細未記載)'}</div>
            </li>
          )
        })}
      </ul>

      <h4>根本原因候補（{result.root_cause_candidates.length}）</h4>
      <ul className="candidates candidates-grid">
        {result.root_cause_candidates.map((c, i) => (
          <li key={i}>
            <span className={`badge cat-${c.category}`}>{c.category}</span>
            <div className="summary-text">{c.summary}</div>
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

      {/* ラウンド単位リソース消費 (Phase D) */}
      {result.round_metrics.length > 0 && <RoundMetricsView rounds={result.round_metrics} />}
    </>
  )
}

interface StageResultViewProps {
  stage: StageOutput
  topology: TopologyDef
}

function StageResultView({ stage, topology }: StageResultViewProps) {
  // StageOutput を AnalysisResult 風に詰めて DelegationHistoryView に渡す簡易ラッパ
  const fakeResult: AnalysisResult = {
    schema_version: 'v0.1',
    trace_id: stage.trace_id,
    config_id: 'config4',
    input_log_ref: '',
    root_cause_candidates: stage.root_cause_candidates,
    recommended_actions: stage.recommended_actions,
    confidence: stage.confidence,
    metrics: { tokens_in: stage.tokens_in, tokens_out: stage.tokens_out, cost_usd: 0, latency_ms_p50: 0, latency_ms_total: stage.latency_ms_total, compression_ratio: 0 },
    info_loss_flags: [],
    execution_graph_nodes: [],
    execution_graph_edges: [],
    delegation_rounds: stage.delegation_rounds,
    delegation_max_rounds: 0,
    delegation_history: stage.delegation_history,
    suspected_node_ids: stage.suspected_node_ids,
    suspected_node_findings: stage.suspected_node_findings,
    stage_outputs: [],
    audit_report: null,
    round_metrics: stage.round_metrics,
  }
  return (
    <>
      <div className="summary-grid">
        <div className="summary-card"><div className="summary-label">確信度</div><div className="summary-value">{stage.confidence.toFixed(2)}</div></div>
        <div className="summary-card"><div className="summary-label">tokens (in/out)</div><div className="summary-value">{stage.tokens_in.toLocaleString()} / {stage.tokens_out.toLocaleString()}</div></div>
        <div className="summary-card"><div className="summary-label">レイテンシ</div><div className="summary-value">{(stage.latency_ms_total / 1000).toFixed(1)}s</div></div>
        <div className="summary-card"><div className="summary-label">ラウンド</div><div className="summary-value">{stage.delegation_rounds}</div></div>
      </div>
      <p className="stage-summary-text">{stage.summary || '(要約なし)'}</p>
      <h4>このステージの障害候補ノード（{stage.suspected_node_findings.length}）</h4>
      <ul className="suspected-detail-list">
        {topology.nodes.filter(n => stage.suspected_node_ids.includes(n.id)).map(n => {
          const f = stage.suspected_node_findings.find(x => x.node_id === n.id)
          return (
            <li key={n.id} className={`suspected-detail sev-${f?.severity || 'unknown'}`}>
              <div className="suspected-detail-header">
                <span className="suspected-id">{n.id}</span>
                {n.type && <span className="suspected-type">[{n.type}]</span>}
                {f?.severity && <span className={`severity-badge sev-${f.severity}`}>{f.severity === 'primary' ? '直接原因' : f.severity === 'secondary' ? '影響を受けた側' : '参考'}</span>}
              </div>
              <div className="suspected-detail-summary">{f?.summary || '(詳細未記載)'}</div>
            </li>
          )
        })}
      </ul>
      <h4>このステージの委譲チェーン</h4>
      <DelegationHistoryView result={fakeResult} />
      {stage.round_metrics.length > 0 && <RoundMetricsView rounds={stage.round_metrics} />}
    </>
  )
}

// 既存トポロジー解析タブの NodeEditor / NodeList と機能等価。
// 別タブで独立進化させるため別シンボルで持つ (将来両者を共通化する余地あり)。

interface CfNodeEditorProps {
  node: TopologyNode
  logs: NodeAttachment[]
  configs: NodeAttachment[]
  sampleLogs: LogEntry[]
  showConfigForm: boolean
  showLogForm: boolean
  onUpdate: (patch: Partial<TopologyNode>) => void
  onRename: (newId: string) => void
  onAddLog: () => void
  onUpdateLog: (i: number, patch: Partial<NodeAttachment>) => void
  onRemoveLog: (i: number) => void
  onAddConfig: () => void
  onUpdateConfig: (i: number, patch: Partial<NodeAttachment>) => void
  onRemoveConfig: (i: number) => void
  onDropLogFiles: (files: FileList) => void
  onDropConfigFiles: (files: FileList) => void
  onLoadSample: (name: string) => void
  onDelete: () => void
  isSuspected: boolean
  allNodes: TopologyNode[]
}
function CfNodeEditor(props: CfNodeEditorProps) {
  const { node, logs, configs, sampleLogs, showConfigForm, showLogForm, onUpdate, onRename, onAddLog, onUpdateLog, onRemoveLog,
          onAddConfig, onUpdateConfig, onRemoveConfig, onDropLogFiles, onDropConfigFiles, onLoadSample, onDelete, isSuspected, allNodes } = props
  const [idDraft, setIdDraft] = useState(node.id)
  useEffect(() => setIdDraft(node.id), [node.id])
  const idTaken = idDraft !== node.id && allNodes.some(n => n.id === idDraft)
  return (
    <div className="node-editor">
      <div className="node-editor-header">
        <h3>ノード編集{isSuspected && <span className="node-suspected-pill">⚠ 障害候補</span>}</h3>
        <button className="btn-danger" onClick={onDelete}>削除</button>
      </div>
      <label className="field"><span>id</span>
        <input type="text" value={idDraft}
          onChange={e => setIdDraft(e.target.value)}
          onBlur={() => { if (!idTaken && idDraft !== node.id) onRename(idDraft); else setIdDraft(node.id) }} />
        {idTaken && <span className="field-error">同じ id が既にあります</span>}
      </label>
      <label className="field"><span>type</span><input type="text" value={node.type} onChange={e => onUpdate({ type: e.target.value })} /></label>
      <label className="field"><span>label</span><input type="text" value={node.label} onChange={e => onUpdate({ label: e.target.value })} /></label>
      <label className="field"><span>ip</span><input type="text" value={node.ip} onChange={e => onUpdate({ ip: e.target.value })} /></label>

      {showLogForm && (
        <CfAttachmentSection title="ログファイル" kind="log" items={logs}
          onAdd={onAddLog} onUpdate={onUpdateLog} onRemove={onRemoveLog} onDropFiles={onDropLogFiles}
          extraTopRow={
            <select defaultValue="" onChange={e => { if (e.target.value) { onLoadSample(e.target.value); e.target.value = '' } }}>
              <option value="">＋ samples/logs/ から追加...</option>
              {sampleLogs.map(l => <option key={l.name} value={l.name}>{l.name} ({l.lines} 行)</option>)}
            </select>
          } />
      )}

      {showConfigForm && (
        <CfAttachmentSection title="設定ファイル (Config)" kind="config" items={configs}
          onAdd={onAddConfig} onUpdate={onUpdateConfig} onRemove={onRemoveConfig} onDropFiles={onDropConfigFiles} />
      )}
    </div>
  )
}

interface CfAttachmentSectionProps {
  title: string
  kind: 'log' | 'config'
  items: NodeAttachment[]
  onAdd: () => void
  onUpdate: (i: number, patch: Partial<NodeAttachment>) => void
  onRemove: (i: number) => void
  onDropFiles?: (files: FileList) => void
  extraTopRow?: React.ReactNode
}
function CfAttachmentSection({ title, kind, items, onAdd, onUpdate, onRemove, onDropFiles, extraTopRow }: CfAttachmentSectionProps) {
  const [dragOver, setDragOver] = useState(false)
  const dndProps = onDropFiles ? {
    onDragOver: (e: React.DragEvent) => {
      if (Array.from(e.dataTransfer.types).includes('Files')) { e.preventDefault(); setDragOver(true) }
    },
    onDragLeave: (e: React.DragEvent) => { if (e.currentTarget === e.target) setDragOver(false) },
    onDrop: (e: React.DragEvent) => {
      if (!Array.from(e.dataTransfer.types).includes('Files')) return
      e.preventDefault(); setDragOver(false)
      if (e.dataTransfer.files.length > 0) onDropFiles(e.dataTransfer.files)
    },
  } : {}
  return (
    <div className={`attach-section attach-${kind}${dragOver ? ' drag-over' : ''}`} {...dndProps}>
      <div className="attach-header">
        <h4>{title} <span className="attach-count">({items.length})</span></h4>
        <button type="button" className="btn-add-attach" onClick={onAdd}>＋ 追加</button>
      </div>
      {extraTopRow && <div className="attach-extra-row">{extraTopRow}</div>}
      {onDropFiles && <div className="attach-dnd-hint muted">ファイルをここにドラッグ＆ドロップで追加</div>}
      {items.length === 0 && <div className="attach-empty">（未設定）</div>}
      {items.map((a, i) => (
        <div key={i} className="attach-item">
          <div className="attach-item-row">
            <input type="text" className="attach-name"
              placeholder={kind === 'log' ? 'ファイル名 (例: fw-syslog.log)' : 'ファイル名 (例: fw-policy.conf)'}
              value={a.name} onChange={e => onUpdate(i, { name: e.target.value })} />
            <button type="button" className="btn-danger btn-small" onClick={() => onRemove(i)}>削除</button>
          </div>
          <textarea className="attach-content"
            placeholder={kind === 'log' ? 'ログ内容を貼り付け' : '設定内容を貼り付け'}
            value={a.content} onChange={e => onUpdate(i, { content: e.target.value })} rows={6} />
        </div>
      ))}
    </div>
  )
}

interface CfNodeListProps {
  nodes: TopologyNode[]
  nodeLogs: NodeAttachments
  nodeConfigs: NodeAttachments
  severityById: Map<string, string>
  showConfigForm: boolean
  showLogForm: boolean
  onSelect: (id: string) => void
}
function CfNodeList({ nodes, nodeLogs, nodeConfigs, severityById, showConfigForm, showLogForm, onSelect }: CfNodeListProps) {
  if (nodes.length === 0) return null
  const cf = (list: NodeAttachment[] | undefined) => (list ?? []).reduce((n, a) => a.content.trim().length > 0 ? n + 1 : n, 0)
  return (
    <ul className="node-list">
      {nodes.map(n => {
        const lc = cf(nodeLogs[n.id]), cc = cf(nodeConfigs[n.id])
        const sev = severityById.get(n.id) ?? ''
        const sevClass = sev === 'primary' ? 'sev-primary' : sev === 'secondary' ? 'sev-secondary' : severityById.has(n.id) && sev === '' ? 'sev-primary' : ''
        return (
          <li key={n.id} className={['node-list-item', sevClass].filter(Boolean).join(' ')} onClick={() => onSelect(n.id)}>
            <span className="node-list-id">{n.id}</span>
            {n.type && <span className="node-list-type">[{n.type}]</span>}
            <span className="node-list-counts">
              {showLogForm && lc > 0 ? <span className="cnt-log">log×{lc}</span> : null}
              {showConfigForm && cc > 0 ? <span className="cnt-cfg">cfg×{cc}</span> : null}
              {(!showLogForm || lc === 0) && (!showConfigForm || cc === 0) && <span className="cnt-empty">○</span>}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
