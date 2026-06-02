import { useState, useEffect, useCallback, useRef } from 'react'
import { BuiltinConfigCanvas } from './BuiltinConfigCanvas'
import { ConfirmationModal } from './ConfirmationModal'
import { DelegationHistoryView, nodeLabel } from './DelegationHistoryView'
import { GraphView } from './GraphView'
import { LogManager } from './LogManager'
import { PipelineBuilder } from './PipelineBuilder'
import { RunHistoryView } from './RunHistoryView'
import { TopologyAnalysis } from './TopologyAnalysis'
import { ConfigLogAnalysis } from './ConfigLogAnalysis'
import type {
  AnalysisResult,
  ConfigEntry,
  DelegationEvent,
  LogEntry,
  SavedConfigDTO,
  SlotInfo,
  SSEEvent,
} from './types'
import './App.css'

const API_BASE = 'http://localhost:8000'

type Mode = 'single' | 'compare' | 'builder' | 'logs' | 'history' | 'topology' | 'config-log'

function ResultDetails({ result }: { result: AnalysisResult }) {
  return (
    <>
      <DelegationHistoryView result={result} />
      <h3>根本原因候補（{result.root_cause_candidates.length}）</h3>
      <ul className="candidates candidates-grid">
        {result.root_cause_candidates.map((c, i) => (
          <li key={i}>
            <span className={`badge cat-${c.category}`}>{c.category}</span>
            <div className="summary-text">{c.summary}</div>
            <details>
              <summary>evidence ({c.evidence.length})</summary>
              <ul className="evidence-list">
                {c.evidence.map((e, j) => <li key={j}><code>{e}</code></li>)}
              </ul>
            </details>
          </li>
        ))}
      </ul>

      <h3>推奨アクション（{result.recommended_actions.length}）</h3>
      <ul className="actions">
        {result.recommended_actions.map((a, i) => (
          <li key={i} className={a.human_judgment_required ? 'requires-human' : ''}>
            <span className={`risk risk-${a.risk_level}`}>{a.risk_level}</span>
            {a.human_judgment_required && <span className="hjr-badge">人間判断必須</span>}
            <span className="action-text">{a.action}</span>
          </li>
        ))}
      </ul>

      {result.info_loss_flags.length > 0 && (
        <details className="info-loss">
          <summary>info_loss_flags ({result.info_loss_flags.length})</summary>
          <ul>
            {result.info_loss_flags.map((f, i) => <li key={i}><code>{f}</code></li>)}
          </ul>
        </details>
      )}
    </>
  )
}

// ─── SSE パーサ ──────────────────────────────────────────────────
// fetch streaming で受け取った Response.body を SSE 形式でパースして
// イベントを 1 件ずつ yield する。トポロジー解析タブも同じパーサを共用する。
export async function* parseSSE(response: Response): AsyncGenerator<SSEEvent> {
  if (!response.body) return
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const chunks = buf.split('\n\n')
    buf = chunks.pop() ?? ''
    for (const chunk of chunks) {
      let kind = 'message'
      let dataText = ''
      for (const line of chunk.split('\n')) {
        if (line.startsWith('event:')) kind = line.slice(6).trim()
        else if (line.startsWith('data:')) dataText += line.slice(5).trim()
      }
      if (!dataText) continue
      try {
        const data = JSON.parse(dataText) as Record<string, unknown>
        yield { kind, data }
      } catch {
        // malformed — skip
      }
    }
  }
}

// ─── リアルタイム実行ログ パネル ─────────────────────────────────
function RealtimeStreamView({
  events,
  canAppend,
  onAppendClick,
}: {
  events: SSEEvent[]
  canAppend: boolean
  onAppendClick: () => void
}) {
  const tailRef = useRef<HTMLOListElement | null>(null)
  useEffect(() => {
    if (tailRef.current) {
      tailRef.current.scrollTop = tailRef.current.scrollHeight
    }
  }, [events.length])
  if (events.length === 0) return null

  return (
    <section className="realtime-stream">
      <div className="realtime-header">
        <h3>リアルタイム実行ログ</h3>
        <span className="realtime-count">{events.length} イベント</span>
        {canAppend && (
          <button className="btn-append-log" onClick={onAppendClick}>
            ＋ ログ追加
          </button>
        )}
      </div>
      <ol className="stream-events" ref={tailRef}>
        {events.map((ev, i) => (
          <li key={i} className={`stream-event kind-${ev.kind}`}>
            <span className="stream-kind">{ev.kind}</span>
            <span className="stream-body">{renderEventSummary(ev)}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}

export function renderEventSummary(ev: SSEEvent): React.ReactNode {
  const d = ev.data
  switch (ev.kind) {
    case 'run_started':
      return <code>trace={String(d.trace_id ?? '?').slice(0, 8)}… max_rounds={String(d.rally_max_rounds)}</code>
    case 'run_id_assigned':
      return <code>run_id={String(d.run_id ?? '').slice(0, 8)}…</code>
    case 'orchestrator_start':
      return <em>初手の監視を選択中...</em>
    case 'orchestrator_decision':
      return (
        <>
          初手 = <strong>{nodeLabel(d.to_node as string)}</strong>
          {d.focus_hint ? <> （観点: {String(d.focus_hint)}）</> : null}
        </>
      )
    case 'monitor_start':
      return (
        <>
          <strong>{nodeLabel(d.node as string)}</strong> 実行開始 (round {String(d.round)})
          {d.focus_hint ? <> ← 観点: {String(d.focus_hint)}</> : null}
        </>
      )
    case 'monitor_decision': {
      const findings = (d.findings as Array<{ category: string; summary: string }>) ?? []
      const top = findings[0]
      return (
        <>
          <strong>{nodeLabel(d.from_node as string)}</strong> →
          {' '}
          <strong>{nodeLabel(d.to_node as string)}</strong>
          {' '}<small>(conf {Number(d.confidence ?? 0).toFixed(2)}, {String(d.tokens_in)}/{String(d.tokens_out)} tok)</small>
          {top && <div className="stream-finding">{top.category}: {top.summary}</div>}
          {d.rationale ? <div className="stream-rationale">理由: {String(d.rationale)}</div> : null}
        </>
      )
    }
    case 'log_appended': {
      const content = String(d.content ?? '')
      const preview = content.length > 200 ? content.slice(0, 200) + '…' : content
      return (
        <>
          ＋ 追加ログ投入 <small>(source={String(d.source ?? '?')}, round_added={String(d.round_added ?? 0)}, {content.length} chars)</small>
          <div className="stream-finding">{preview}</div>
        </>
      )
    }
    case 'await_confirmation':
      return <strong>⚠ rally_max_rounds={String(d.rally_max_rounds)} 到達。継続判断を求めています</strong>
    case 'user_decision':
      return <em>ユーザー応答: action={String(d.action)}{d.extend_by ? ` (+${String(d.extend_by)})` : ''}</em>
    case 'max_rounds_finalize':
      return <em>強制 finalize (max_rounds 到達)</em>
    case 'integrator_start':
      return <em>integrator で統合中...</em>
    case 'integrator_done':
      return <>統合完了 (conf {Number(d.confidence ?? 0).toFixed(2)}, 候補 {String(d.candidates)})</>
    case 'final':
      return <em>完了</em>
    case 'error':
      return <span className="stream-error">エラー: {String(d.message ?? d)}</span>
    default:
      return <code>{JSON.stringify(d).slice(0, 200)}</code>
  }
}

// ─── 追加ログ投入モーダル ─────────────────────────────────────
interface AddLogModalProps {
  logs: LogEntry[]
  busy: boolean
  onSubmit: (content: string, source: string) => void
  onClose: () => void
}

function AddLogModal({ logs, busy, onSubmit, onClose }: AddLogModalProps) {
  const [mode, setMode] = useState<'paste' | 'sample'>('paste')
  const [content, setContent] = useState<string>('')
  const [source, setSource] = useState<string>('inline')
  const [selectedSample, setSelectedSample] = useState<string>(logs[0]?.name ?? '')

  const handleSubmit = () => {
    if (mode === 'paste') {
      if (!content.trim()) return
      onSubmit(content, source.trim() || 'inline')
    } else {
      if (!selectedSample) return
      // content 空 + source=ファイル名 でサーバ側読み込み
      onSubmit('', selectedSample)
    }
  }

  const canSubmit =
    !busy &&
    ((mode === 'paste' && content.trim().length > 0) ||
      (mode === 'sample' && !!selectedSample))

  return (
    <div className="modal-overlay">
      <div className="modal append-log-modal">
        <h3>解析中に追加のログを投入</h3>
        <p className="modal-summary">
          次の監視ノード / integrator の入力に含まれます。元ログは変更されません（caching 維持）。
        </p>

        <div className="append-mode-tabs">
          <button
            type="button"
            className={mode === 'paste' ? 'tab active' : 'tab'}
            onClick={() => setMode('paste')}
            disabled={busy}
          >
            テキスト貼り付け
          </button>
          <button
            type="button"
            className={mode === 'sample' ? 'tab active' : 'tab'}
            onClick={() => setMode('sample')}
            disabled={busy || logs.length === 0}
          >
            samples/logs から選択
          </button>
        </div>

        {mode === 'paste' && (
          <>
            <label className="append-field">
              <span>source ラベル (任意)</span>
              <input
                type="text"
                value={source}
                onChange={e => setSource(e.target.value)}
                placeholder="inline / 任意の識別子"
                disabled={busy}
              />
            </label>
            <label className="append-field">
              <span>ログ本文</span>
              <textarea
                value={content}
                onChange={e => setContent(e.target.value)}
                rows={10}
                disabled={busy}
                placeholder="ここにログを貼り付けてください..."
              />
            </label>
          </>
        )}

        {mode === 'sample' && (
          <label className="append-field">
            <span>samples/logs/ から選択</span>
            <select
              value={selectedSample}
              onChange={e => setSelectedSample(e.target.value)}
              disabled={busy}
            >
              {logs.map(l => (
                <option key={l.name} value={l.name}>
                  {l.name}（{l.lines} 行 / {l.bytes.toLocaleString()} bytes）
                </option>
              ))}
            </select>
          </label>
        )}

        <div className="modal-actions">
          <button onClick={handleSubmit} disabled={!canSubmit}>
            投入する
          </button>
          <button onClick={onClose} disabled={busy} className="btn-secondary">
            キャンセル
          </button>
        </div>
      </div>
    </div>
  )
}

function ResultSummaryGrid({ result, langfuseHost }: { result: AnalysisResult; langfuseHost: string | null }) {
  // Langfuse v2 のトレース URL 規約: ${host}/trace/${trace_id}
  // host が null（未設定）ならリンクではなくテキスト表示
  const traceUrl = langfuseHost ? `${langfuseHost}/trace/${result.trace_id}` : null
  return (
    <div className="summary-grid">
      <div className="summary-card">
        <div className="summary-label">構成</div>
        <div className="summary-value">{result.config_id}</div>
      </div>
      <div className="summary-card">
        <div className="summary-label">確信度</div>
        <div className="summary-value">{result.confidence.toFixed(2)}</div>
      </div>
      <div className="summary-card">
        <div className="summary-label">トークン (in / out)</div>
        <div className="summary-value">
          {result.metrics.tokens_in.toLocaleString()} / {result.metrics.tokens_out.toLocaleString()}
        </div>
      </div>
      <div className="summary-card">
        <div className="summary-label">レイテンシ</div>
        <div className="summary-value">{(result.metrics.latency_ms_total / 1000).toFixed(1)}s</div>
      </div>
      {result.metrics.compression_ratio > 0 && (
        <div className="summary-card">
          <div className="summary-label">圧縮率</div>
          <div className="summary-value">{result.metrics.compression_ratio.toFixed(3)}</div>
        </div>
      )}
      <div className="summary-card">
        <div className="summary-label">trace_id</div>
        <div className="summary-value mono small">
          {traceUrl ? (
            <a href={traceUrl} target="_blank" rel="noopener noreferrer" className="trace-link">
              {result.trace_id} ↗
            </a>
          ) : (
            result.trace_id
          )}
        </div>
      </div>
    </div>
  )
}

function App() {
  const [mode, setMode] = useState<Mode>('single')

  const [configList, setConfigList] = useState<ConfigEntry[]>([])
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [selectedLog, setSelectedLog] = useState<string>('')
  const [error, setError] = useState<string | null>(null)
  // Langfuse UI への直リンク用ホスト URL（バックエンドから取得、未設定なら null）
  const [langfuseHost, setLangfuseHost] = useState<string | null>(null)

  // Single mode
  const [selectedConfig, setSelectedConfig] = useState<string>('')
  const [singleResult, setSingleResult] = useState<AnalysisResult | null>(null)
  const [singleRunning, setSingleRunning] = useState(false)
  const [singleElapsedSec, setSingleElapsedSec] = useState<number>(0)

  // Editor state
  const [slots, setSlots] = useState<SlotInfo[]>([])
  // editorOverrides: slot_id → 上書き値（プロンプト / モデル）。
  // 実際に保存・送信するときは「デフォルトと異なる slot」だけに絞る
  const [editorOverrides, setEditorOverrides] = useState<Record<string, string>>({})
  const [editorModelOverrides, setEditorModelOverrides] = useState<Record<string, string>>({})
  // 「現在の保存状態」: 編集前のスナップショット（復元ボタンの戻り先）
  const [loadedOverrides, setLoadedOverrides] = useState<Record<string, string>>({})
  const [loadedModelOverrides, setLoadedModelOverrides] = useState<Record<string, string>>({})
  const [saveName, setSaveName] = useState<string>('')
  const [savingConfig, setSavingConfig] = useState<boolean>(false)

  // Compare mode
  const [compareResults, setCompareResults] = useState<Record<string, AnalysisResult>>({})
  const [compareErrors, setCompareErrors] = useState<Record<string, string>>({})
  const [compareRunningSet, setCompareRunningSet] = useState<Set<string>>(new Set())
  const [compareElapsed, setCompareElapsed] = useState<Record<string, number>>({})
  const [compareSelected, setCompareSelected] = useState<Set<string>>(new Set())

  // Builder mode（構成設計）
  const [builderEditingId, setBuilderEditingId] = useState<string | null>('__new__')

  // 構成4 専用ランタイムパラメータ
  const [rallyMaxRounds, setRallyMaxRounds] = useState<number>(3)

  // 構成4 SSE ストリーミング状態
  const [streamEvents, setStreamEvents] = useState<SSEEvent[]>([])
  const [streamRunId, setStreamRunId] = useState<string | null>(null)
  const [pendingConfirmation, setPendingConfirmation] = useState<{
    round: number
    rally_max_rounds: number
    delegation_history: DelegationEvent[]
  } | null>(null)
  const [decisionBusy, setDecisionBusy] = useState<boolean>(false)
  // 追加ログ投入モーダル
  const [addLogOpen, setAddLogOpen] = useState<boolean>(false)
  const [appendBusy, setAppendBusy] = useState<boolean>(false)

  const selectedConfigEntry = configList.find(c => c.id === selectedConfig)
  const selectedBaseConfig = selectedConfigEntry?.base_config ?? ''
  const isUserConfig = selectedConfigEntry?.type === 'user'
  const isViewOnlyConfig = selectedConfigEntry?.type === 'builtin_view_only'
  const userConfigId = isUserConfig ? Number(selectedConfig.split(':')[1]) : null

  // 保存・送信用の overrides: デフォルトと異なる slot だけに絞る
  function effectivePromptOverrides(): Record<string, string> {
    const out: Record<string, string> = {}
    for (const slot of slots) {
      const v = editorOverrides[slot.slot_id]
      if (v != null && v !== slot.default_prompt) out[slot.slot_id] = v
    }
    return out
  }

  function effectiveModelOverrides(): Record<string, string> {
    const out: Record<string, string> = {}
    for (const slot of slots) {
      if (slot.allowed_models.length === 0) continue
      const v = editorModelOverrides[slot.slot_id]
      if (v != null && v !== slot.default_model) out[slot.slot_id] = v
    }
    return out
  }

  // 「変更あり」判定: 現在の effective が loaded と一致しなければ変更あり
  const hasUnsavedChanges = (() => {
    const ep = effectivePromptOverrides()
    const em = effectiveModelOverrides()
    const epKeys = Object.keys(ep)
    const emKeys = Object.keys(em)
    const lpKeys = Object.keys(loadedOverrides)
    const lmKeys = Object.keys(loadedModelOverrides)
    if (epKeys.length !== lpKeys.length || emKeys.length !== lmKeys.length) return true
    if (epKeys.some(k => ep[k] !== loadedOverrides[k])) return true
    if (emKeys.some(k => em[k] !== loadedModelOverrides[k])) return true
    return false
  })()

  const loadConfigs = useCallback(() => {
    return fetch(`${API_BASE}/api/configs`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then((d: { configs: ConfigEntry[] }) => {
        setConfigList(d.configs)
        return d.configs
      })
      .catch(e => {
        setError(`構成リスト取得失敗: ${e.message}`)
        return []
      })
  }, [])

  const loadLogs = useCallback(() => {
    return fetch(`${API_BASE}/api/logs`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then((d: { logs: LogEntry[] }) => {
        setLogs(d.logs)
        // 削除されたログを selectedLog に持っていた場合は別のログに切り替え
        setSelectedLog(prev => {
          if (d.logs.some(l => l.name === prev)) return prev
          return d.logs[0]?.name ?? ''
        })
        return d.logs
      })
      .catch(e => {
        setError(`ログ一覧取得失敗: ${e.message}`)
        return [] as LogEntry[]
      })
  }, [])

  useEffect(() => {
    loadConfigs().then(configs => {
      if (configs.length > 0 && !selectedConfig) setSelectedConfig(configs[0].id)
      if (configs.length > 0) {
        const builtinIds = configs.filter(c => c.type === 'builtin').map(c => c.id)
        setCompareSelected(new Set(builtinIds))
      }
    })
    loadLogs()
    // Langfuse host を取得（失敗してもクリティカルではないので catch して無視）
    fetch(`${API_BASE}/api/runtime-config`)
      .then(r => (r.ok ? r.json() : null))
      .then((d: { langfuse_host: string | null } | null) => {
        if (d?.langfuse_host) setLangfuseHost(d.langfuse_host)
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // base_config が変わったら slot 一覧を取得
  useEffect(() => {
    if (!selectedBaseConfig) return
    fetch(`${API_BASE}/api/prompt-slots/${selectedBaseConfig}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then((d: { slots: SlotInfo[] }) => setSlots(d.slots))
      .catch(e => setError(`slot 取得失敗: ${e.message}`))
  }, [selectedBaseConfig])

  // 構成選択が変わったらエディタの値を再ロード
  useEffect(() => {
    if (!selectedConfigEntry) return
    if (selectedConfigEntry.type === 'user') {
      const id = Number(selectedConfigEntry.id.split(':')[1])
      fetch(`${API_BASE}/api/configs/saved`)
        .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
        .then((d: { configs: SavedConfigDTO[] }) => {
          const sc = d.configs.find(c => c.id === id)
          if (sc) {
            setEditorOverrides({ ...sc.overrides })
            setLoadedOverrides({ ...sc.overrides })
            setEditorModelOverrides({ ...(sc.model_overrides ?? {}) })
            setLoadedModelOverrides({ ...(sc.model_overrides ?? {}) })
            setSaveName(sc.name)
          }
        })
        .catch(e => setError(`保存済み構成読み込み失敗: ${e.message}`))
    } else {
      // builtin: editor は空 = デフォルト
      setEditorOverrides({})
      setLoadedOverrides({})
      setEditorModelOverrides({})
      setLoadedModelOverrides({})
      setSaveName('')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedConfig])

  useEffect(() => {
    if (!singleRunning) {
      setSingleElapsedSec(0)
      return
    }
    const start = Date.now()
    const id = setInterval(() => {
      setSingleElapsedSec(Math.floor((Date.now() - start) / 1000))
    }, 500)
    return () => clearInterval(id)
  }, [singleRunning])

  useEffect(() => {
    if (compareRunningSet.size === 0) return
    const startTimes: Record<string, number> = {}
    compareRunningSet.forEach(c => {
      if (compareElapsed[c] === undefined) startTimes[c] = Date.now()
    })
    const id = setInterval(() => {
      setCompareElapsed(prev => {
        const next = { ...prev }
        compareRunningSet.forEach(c => {
          if (startTimes[c] !== undefined) {
            next[c] = Math.floor((Date.now() - startTimes[c]) / 1000)
          }
        })
        return next
      })
    }, 500)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compareRunningSet])

  const handlePromptSlotChange = (slotId: string, value: string) => {
    setEditorOverrides(prev => ({ ...prev, [slotId]: value }))
  }

  const handleModelSlotChange = (slotId: string, value: string) => {
    setEditorModelOverrides(prev => ({ ...prev, [slotId]: value }))
  }

  const handleResetEditor = () => {
    setEditorOverrides({ ...loadedOverrides })
    setEditorModelOverrides({ ...loadedModelOverrides })
  }

  const handleSaveAsNew = async () => {
    if (!saveName.trim()) {
      setError('保存名を入れてください')
      return
    }
    const overrides = effectivePromptOverrides()
    const modelOverrides = effectiveModelOverrides()
    if (Object.keys(overrides).length === 0 && Object.keys(modelOverrides).length === 0) {
      setError('変更が無いため保存しません（既定との差分が必要）')
      return
    }
    setSavingConfig(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/api/configs/saved`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: saveName,
          base_config: selectedBaseConfig,
          overrides,
          model_overrides: modelOverrides,
        }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const created: SavedConfigDTO = await r.json()
      const fresh = await loadConfigs()
      const newId = `user:${created.id}`
      if (fresh.some(c => c.id === newId)) setSelectedConfig(newId)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSavingConfig(false)
    }
  }

  const handleOverwrite = async () => {
    if (userConfigId === null) return
    const overrides = effectivePromptOverrides()
    const modelOverrides = effectiveModelOverrides()
    setSavingConfig(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/api/configs/saved/${userConfigId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ overrides, model_overrides: modelOverrides }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      setLoadedOverrides({ ...overrides })
      setLoadedModelOverrides({ ...modelOverrides })
      await loadConfigs()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSavingConfig(false)
    }
  }

  const handleDelete = async () => {
    if (userConfigId === null) return
    if (!confirm(`構成 "${saveName}" を削除しますか？`)) return
    try {
      const r = await fetch(`${API_BASE}/api/configs/saved/${userConfigId}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const fresh = await loadConfigs()
      setSelectedConfig(fresh.find(c => c.type === 'builtin')?.id ?? '')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const handleSingleRun = async () => {
    if (!selectedConfigEntry) return
    setSingleRunning(true)
    setError(null)
    setSingleResult(null)
    setStreamEvents([])
    setStreamRunId(null)
    setPendingConfirmation(null)
    try {
      const body: Record<string, unknown> = { log_name: selectedLog, config: selectedConfig }
      // builtin で編集中なら ad-hoc overrides を送る
      if (selectedConfigEntry.type === 'builtin') {
        const ov = effectivePromptOverrides()
        const mov = effectiveModelOverrides()
        if (Object.keys(ov).length > 0) body.overrides = ov
        if (Object.keys(mov).length > 0) body.model_overrides = mov
      }
      // 構成4（rally）はランタイムパラメータも送る
      if (selectedBaseConfig === 'config4') {
        body.rally_max_rounds = rallyMaxRounds
      }

      // 構成4 のみ SSE ストリーミング経路を使う
      if (selectedBaseConfig === 'config4') {
        const r = await fetch(`${API_BASE}/api/runs/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
        for await (const ev of parseSSE(r)) {
          setStreamEvents(prev => [...prev, ev])
          if (ev.kind === 'run_id_assigned') {
            setStreamRunId(String(ev.data.run_id ?? ''))
          } else if (ev.kind === 'await_confirmation') {
            setPendingConfirmation({
              round: Number(ev.data.round ?? 0),
              rally_max_rounds: Number(ev.data.rally_max_rounds ?? 0),
              delegation_history: (ev.data.delegation_history as DelegationEvent[]) ?? [],
            })
          } else if (ev.kind === 'user_decision') {
            setPendingConfirmation(null)
          } else if (ev.kind === 'final') {
            setSingleResult(ev.data.result as AnalysisResult)
          } else if (ev.kind === 'error') {
            const msg = (ev.data as Record<string, unknown>).message ?? JSON.stringify(ev.data)
            setError(`stream error: ${String(msg)}`)
          }
        }
        return
      }

      // それ以外は従来通り同期 /api/runs
      const r = await fetch(`${API_BASE}/api/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      const data: AnalysisResult = await r.json()
      setSingleResult(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSingleRunning(false)
      setStreamRunId(null)
    }
  }

  const handleAppendLog = async (content: string, source: string) => {
    if (!streamRunId) return
    setAppendBusy(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/api/runs/${streamRunId}/append-log`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, source }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      setAddLogOpen(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setAppendBusy(false)
    }
  }

  const handleDecision = async (action: 'continue' | 'stop', extendBy?: number) => {
    if (!streamRunId) return
    setDecisionBusy(true)
    try {
      const body: Record<string, unknown> = { action }
      if (action === 'continue' && extendBy && extendBy > 0) body.extend_by = extendBy
      const r = await fetch(`${API_BASE}/api/runs/${streamRunId}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
      // pendingConfirmation は user_decision SSE イベント側でクリアされる
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDecisionBusy(false)
    }
  }

  const toggleCompareSelected = (id: string) => {
    setCompareSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleCompareRun = () => {
    setError(null)
    setCompareResults({})
    setCompareErrors({})
    setCompareElapsed({})
    const targets = configList.filter(c => compareSelected.has(c.id))
    setCompareRunningSet(new Set(targets.map(c => c.id)))
    targets.forEach(c => {
      fetch(`${API_BASE}/api/runs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ log_name: selectedLog, config: c.id }),
      })
        .then(async r => {
          if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`)
          return r.json() as Promise<AnalysisResult>
        })
        .then(data => setCompareResults(prev => ({ ...prev, [c.id]: data })))
        .catch(e => setCompareErrors(prev => ({ ...prev, [c.id]: e instanceof Error ? e.message : String(e) })))
        .finally(() => {
          setCompareRunningSet(prev => {
            const next = new Set(prev)
            next.delete(c.id)
            return next
          })
        })
    })
  }

  const compareDoneCount = Object.keys(compareResults).length + Object.keys(compareErrors).length
  const isCompareRunning = compareRunningSet.size > 0
  const compareTargetsCount = compareSelected.size

  return (
    <div className="container">
      <header>
        <h1>log-analyzer 管理 UI<span className="version">MVP / Phase 2 W6</span></h1>
        <p className="subtitle">
          ログを選び、構成（builtin / ユーザー定義）で分析を実行する。エージェント組織図 + 比較表 + 段階別プロンプト編集対応。
        </p>
      </header>

      <div className="mode-tabs">
        {/* 構成比較 / 構成設計（pipeline）/ トポロジー解析 タブは非表示
            (コードは残置。再表示する場合はこのコメント内のボタンを戻す) */}
        <button
          onClick={() => setMode('single')}
          className={mode === 'single' ? 'tab active' : 'tab'}
          disabled={singleRunning || isCompareRunning}
        >
          単一実行
        </button>
        <button
          onClick={() => setMode('logs')}
          className={mode === 'logs' ? 'tab active' : 'tab'}
          disabled={singleRunning || isCompareRunning}
        >
          ログ管理
        </button>
        <button
          onClick={() => setMode('history')}
          className={mode === 'history' ? 'tab active' : 'tab'}
          disabled={singleRunning || isCompareRunning}
        >
          実行履歴
        </button>
        <button
          onClick={() => setMode('config-log')}
          className={mode === 'config-log' ? 'tab active' : 'tab'}
          disabled={singleRunning || isCompareRunning}
        >
          config-log 解析
        </button>
      </div>

      {mode === 'builder' && (
        <PipelineBuilder
          configList={configList}
          logs={logs}
          selectedLog={selectedLog}
          onSelectedLogChange={setSelectedLog}
          onConfigsRefresh={loadConfigs}
          editingConfigId={builderEditingId}
          onEditingConfigIdChange={setBuilderEditingId}
        />
      )}

      {mode === 'logs' && (
        <LogManager logs={logs} onLogsChange={loadLogs} />
      )}

      {mode === 'history' && (
        <RunHistoryView
          configList={configList}
          logs={logs}
          langfuseHost={langfuseHost}
        />
      )}

      {mode === 'topology' && (
        <TopologyAnalysis
          configList={configList}
          logs={logs}
          parseSSE={parseSSE}
          renderEventSummary={renderEventSummary}
          langfuseHost={langfuseHost}
        />
      )}

      {mode === 'config-log' && (
        <ConfigLogAnalysis
          configList={configList}
          logs={logs}
          parseSSE={parseSSE}
          renderEventSummary={renderEventSummary}
          langfuseHost={langfuseHost}
        />
      )}

      {mode === 'single' && (
        <>
          <section className="controls">
            <label>
              ログ
              <select value={selectedLog} onChange={e => setSelectedLog(e.target.value)} disabled={singleRunning}>
                {logs.map(l => (
                  <option key={l.name} value={l.name}>
                    {l.name}（{l.lines} 行 / {l.bytes.toLocaleString()} bytes）
                  </option>
                ))}
              </select>
            </label>
            <label>
              構成
              <select value={selectedConfig} onChange={e => setSelectedConfig(e.target.value)} disabled={singleRunning}>
                <optgroup label="builtin">
                  {configList.filter(c => c.type === 'builtin').map(c => (
                    <option key={c.id} value={c.id}>{c.label}</option>
                  ))}
                </optgroup>
                {configList.some(c => c.type === 'user') && (
                  <optgroup label="ユーザー定義">
                    {configList.filter(c => c.type === 'user').map(c => (
                      <option key={c.id} value={c.id}>{c.label}</option>
                    ))}
                  </optgroup>
                )}
              </select>
            </label>
            <button
              onClick={handleSingleRun}
              disabled={singleRunning || !selectedLog || !selectedConfig || isViewOnlyConfig}
              className="run-button"
            >
              {singleRunning ? `実行中… ${singleElapsedSec}s` : isViewOnlyConfig ? '実行不可（専用タブから）' : '実行'}
            </button>
          </section>

          {selectedConfigEntry && selectedBaseConfig === 'config5' && (
            <div className="config5-hint">
              この構成は <strong>config5（user_pipeline）</strong> ベースです。プロンプトとモデルの編集は
              <strong>「構成設計（pipeline）」</strong>タブで行ってください。
            </div>
          )}

          {isViewOnlyConfig && (
            <div className="config5-hint view-only-hint">
              この構成は <strong>表示専用</strong> です（実行不可）。
              構成図を参考にしつつ、解析の実行は
              <button className="link-button" onClick={() => setMode('config-log')}>
                「config-log 解析」タブ
              </button>
              から行ってください。
            </div>
          )}

          {selectedConfigEntry && selectedBaseConfig === 'config4' && (
            <section className="rally-controls">
              <div className="rally-controls-title">
                ラリー制御（config4 専用）
              </div>
              <div className="rally-controls-row">
                <label className="rally-control">
                  <span className="rally-label">最大ラウンド数</span>
                  <input
                    type="number"
                    min={1}
                    max={10}
                    value={rallyMaxRounds}
                    onChange={e => setRallyMaxRounds(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
                    disabled={singleRunning}
                  />
                  <span className="rally-hint">
                    委譲チェーンの上限（1〜10）。到達すると確認モーダルが表示されます
                  </span>
                </label>
              </div>
            </section>
          )}

          {selectedConfigEntry && selectedBaseConfig !== 'config5' && (
            <section className="builtin-editor">
              <div className="builtin-editor-header">
                <span className="builtin-editor-title">
                  ワークフロー（{selectedBaseConfig}）
                  {hasUnsavedChanges && <span className="modified-badge">変更あり</span>}
                  {isUserConfig && <span className="hint">編集中: {saveName}</span>}
                </span>
                <span className="builtin-editor-hint">
                  ノードをクリックしてプロンプト/モデルを編集
                </span>
              </div>

              <BuiltinConfigCanvas
                key={selectedConfig}
                baseConfig={selectedBaseConfig}
                slots={slots}
                promptOverrides={editorOverrides}
                modelOverrides={editorModelOverrides}
                onPromptChange={handlePromptSlotChange}
                onModelChange={handleModelSlotChange}
                disabled={singleRunning || savingConfig}
              />

              <div className="editor-actions">
                <input
                  type="text"
                  placeholder={isUserConfig ? '保存名（変更で別名保存）' : '保存名（例: my-strict-fw）'}
                  value={saveName}
                  onChange={e => setSaveName(e.target.value)}
                  disabled={singleRunning || savingConfig}
                  className="save-name"
                />
                <button
                  onClick={handleSaveAsNew}
                  disabled={singleRunning || savingConfig || !saveName.trim()}
                >
                  {savingConfig ? '保存中…' : '新規保存'}
                </button>
                {isUserConfig && (
                  <button
                    onClick={handleOverwrite}
                    disabled={singleRunning || savingConfig || !hasUnsavedChanges}
                    className="btn-secondary"
                  >
                    上書き保存
                  </button>
                )}
                {isUserConfig && (
                  <button
                    onClick={handleDelete}
                    disabled={singleRunning || savingConfig}
                    className="btn-delete"
                  >
                    削除
                  </button>
                )}
                <button
                  onClick={handleResetEditor}
                  disabled={singleRunning || savingConfig || !hasUnsavedChanges}
                  className="btn-secondary"
                >
                  復元
                </button>
              </div>
              {isUserConfig && hasUnsavedChanges && (
                <p className="editor-warning">
                  ⚠ 編集中のローカル変更は実行に反映されません。実行前に「上書き保存」または「新規保存」してください。
                </p>
              )}
            </section>
          )}

          {error && (
            <div className="error">
              <strong>エラー:</strong> {error}
            </div>
          )}

          {/* 構成4 SSE ストリーミング中はリアルタイムログを表示 */}
          {streamEvents.length > 0 && (
            <RealtimeStreamView
              events={streamEvents}
              canAppend={singleRunning && !!streamRunId}
              onAppendClick={() => setAddLogOpen(true)}
            />
          )}

          {singleResult && (
            <section className="result">
              <h2>結果</h2>
              <ResultSummaryGrid result={singleResult} langfuseHost={langfuseHost} />

              <h3>エージェント組織図</h3>
              <GraphView
                nodes={singleResult.execution_graph_nodes}
                edges={singleResult.execution_graph_edges}
              />

              <ResultDetails result={singleResult} />
            </section>
          )}
        </>
      )}

      {/* 上限到達時の確認モーダル（全モード共通の overlay） */}
      {pendingConfirmation && (
        <ConfirmationModal
          round={pendingConfirmation.round}
          maxRounds={pendingConfirmation.rally_max_rounds}
          history={pendingConfirmation.delegation_history}
          busy={decisionBusy}
          onContinue={extendBy => handleDecision('continue', extendBy)}
          onStop={() => handleDecision('stop')}
        />
      )}

      {/* 追加ログ投入モーダル（構成4 ストリーム中のみ表示可能） */}
      {addLogOpen && (
        <AddLogModal
          logs={logs}
          busy={appendBusy}
          onSubmit={handleAppendLog}
          onClose={() => setAddLogOpen(false)}
        />
      )}

      {mode === 'compare' && (
        <>
          <section className="controls">
            <label>
              ログ
              <select value={selectedLog} onChange={e => setSelectedLog(e.target.value)} disabled={isCompareRunning}>
                {logs.map(l => (
                  <option key={l.name} value={l.name}>
                    {l.name}（{l.lines} 行 / {l.bytes.toLocaleString()} bytes）
                  </option>
                ))}
              </select>
            </label>
            <button
              onClick={handleCompareRun}
              disabled={isCompareRunning || !selectedLog || compareTargetsCount === 0}
              className="run-button"
            >
              {isCompareRunning
                ? `実行中… ${compareDoneCount}/${compareTargetsCount} 完了`
                : `${compareTargetsCount} 構成同時実行`}
            </button>
          </section>

          <section className="compare-selector">
            <div className="compare-selector-title">比較対象</div>
            <div className="compare-checkboxes">
              {configList.map(c => (
                <label key={c.id} className="compare-check">
                  <input
                    type="checkbox"
                    checked={compareSelected.has(c.id)}
                    onChange={() => toggleCompareSelected(c.id)}
                    disabled={isCompareRunning}
                  />
                  <span className={c.type === 'user' ? 'user-config' : ''}>{c.label}</span>
                </label>
              ))}
            </div>
          </section>

          {error && (
            <div className="error">
              <strong>エラー:</strong> {error}
            </div>
          )}

          {(Object.keys(compareResults).length > 0 || Object.keys(compareErrors).length > 0 || isCompareRunning) && (
            <>
              <section className="compare-grid">
                {configList.filter(c => compareSelected.has(c.id)).map(c => {
                  const r = compareResults[c.id]
                  const err = compareErrors[c.id]
                  const running = compareRunningSet.has(c.id)
                  return (
                    <div key={c.id} className="compare-card">
                      <div className="compare-card-header">
                        <span className="config-name">{c.label}</span>
                        {running && <span className="status-badge running">実行中 {compareElapsed[c.id] ?? 0}s</span>}
                        {!running && r && <span className="status-badge done">完了 {(r.metrics.latency_ms_total / 1000).toFixed(1)}s</span>}
                        {!running && err && <span className="status-badge error">エラー</span>}
                      </div>
                      {r && (
                        <div className="compare-card-body">
                          <div className="kv">
                            <span className="k">確信度</span>
                            <span className="v">{r.confidence.toFixed(2)}</span>
                          </div>
                          <div className="kv">
                            <span className="k">tokens</span>
                            <span className="v">{r.metrics.tokens_in.toLocaleString()} / {r.metrics.tokens_out.toLocaleString()}</span>
                          </div>
                          {r.metrics.compression_ratio > 0 && (
                            <div className="kv">
                              <span className="k">圧縮率</span>
                              <span className="v">{r.metrics.compression_ratio.toFixed(3)}</span>
                            </div>
                          )}
                          <div className="kv">
                            <span className="k">top</span>
                            <span className="v">
                              <span className={`badge cat-${r.root_cause_candidates[0]?.category ?? 'Unknown'}`}>
                                {r.root_cause_candidates[0]?.category ?? '?'}
                              </span>
                            </span>
                          </div>
                          <div className="top-summary">{r.root_cause_candidates[0]?.summary}</div>
                          <details className="card-details">
                            <summary>詳細 + 組織図</summary>
                            <h4>エージェント組織図</h4>
                            <GraphView nodes={r.execution_graph_nodes} edges={r.execution_graph_edges} />
                            <ResultDetails result={r} />
                          </details>
                        </div>
                      )}
                      {err && (
                        <div className="compare-card-body error-body">
                          <code>{err}</code>
                        </div>
                      )}
                    </div>
                  )
                })}
              </section>

              {compareDoneCount === compareTargetsCount && Object.keys(compareResults).length > 0 && (
                <section className="compare-table-section">
                  <h2>比較表</h2>
                  <div className="table-wrap">
                    <table className="compare-table">
                      <thead>
                        <tr>
                          <th>構成</th>
                          <th>確信度</th>
                          <th>tokens_in</th>
                          <th>tokens_out</th>
                          <th>レイテンシ</th>
                          <th>圧縮率</th>
                          <th>top</th>
                        </tr>
                      </thead>
                      <tbody>
                        {configList.filter(c => compareSelected.has(c.id)).map(c => {
                          const r = compareResults[c.id]
                          if (!r) return (
                            <tr key={c.id}><td>{c.label}</td><td colSpan={6}><em>失敗 or 未完了</em></td></tr>
                          )
                          return (
                            <tr key={c.id}>
                              <td>{c.label}</td>
                              <td>{r.confidence.toFixed(2)}</td>
                              <td>{r.metrics.tokens_in.toLocaleString()}</td>
                              <td>{r.metrics.tokens_out.toLocaleString()}</td>
                              <td>{(r.metrics.latency_ms_total / 1000).toFixed(1)}s</td>
                              <td>{r.metrics.compression_ratio > 0 ? r.metrics.compression_ratio.toFixed(3) : '-'}</td>
                              <td><span className={`badge cat-${r.root_cause_candidates[0]?.category ?? 'Unknown'}`}>{r.root_cause_candidates[0]?.category ?? '?'}</span></td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}

export default App
