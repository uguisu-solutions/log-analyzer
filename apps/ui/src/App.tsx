import { useState, useEffect, useCallback } from 'react'
import { BuiltinConfigCanvas } from './BuiltinConfigCanvas'
import { GraphView } from './GraphView'
import { PipelineBuilder } from './PipelineBuilder'
import type {
  AnalysisResult,
  ConfigEntry,
  LogEntry,
  SavedConfigDTO,
  SlotInfo,
} from './types'
import './App.css'

const API_BASE = 'http://localhost:8000'

type Mode = 'single' | 'compare' | 'builder'

function OrchestratorHistoryView({ result }: { result: AnalysisResult }) {
  // 構成4 のみ意味のあるデータ。古い API レスポンスや他構成ではフィールドが
  // undefined / 0 / 空のことがあるので防御的に読む
  const rounds = result.orchestrator_rounds ?? 0
  const maxRounds = result.orchestrator_max_rounds ?? 0
  const history = result.orchestrator_history ?? []
  if (rounds === 0 || history.length === 0) {
    return null
  }
  const totalLLMRounds = history.filter(d => !d.forced || d.action === 'invoke').length
  const forcedFinalize = history.some(d => d.forced && d.action === 'finalize')
  const invokeRounds = history.filter(d => d.action === 'invoke').length
  return (
    <section className="orchestrator-history">
      <h3>オーケストレータ判断履歴（{rounds} ラウンド / 上限 {maxRounds}）</h3>
      <div className="orchestrator-summary">
        <span className="kv">
          <span className="k">LLM 判断回数</span>
          <span className="v">{totalLLMRounds}</span>
        </span>
        <span className="kv">
          <span className="k">監視再呼出</span>
          <span className="v">{invokeRounds} 回</span>
        </span>
        {forcedFinalize && (
          <span className="kv warn">
            <span className="k">⚠</span>
            <span className="v">上限到達で強制 finalize</span>
          </span>
        )}
      </div>
      <ol className="orchestrator-rounds">
        {history.map((d, i) => {
          const invoke = d.invoke ?? []
          const focusHints = d.focus_hints ?? {}
          const focusKeys = Object.keys(focusHints)
          return (
            <li key={i} className={`orch-round action-${d.action}${d.forced ? ' forced' : ''}`}>
              <div className="orch-round-header">
                <span className="round-num">round {d.round}</span>
                <span className={`action-badge action-${d.action}`}>{d.action}</span>
                {d.forced && <span className="forced-badge">forced</span>}
                {d.action === 'invoke' && invoke.length > 0 && (
                  <span className="invoke-list">→ {invoke.join(', ')}</span>
                )}
              </div>
              {d.rationale && <div className="orch-rationale">{d.rationale}</div>}
              {focusKeys.length > 0 && (
                <details className="focus-hints">
                  <summary>focus_hints ({focusKeys.length})</summary>
                  <ul>
                    {focusKeys.map(k => (
                      <li key={k}>
                        <code>{k}</code>: {focusHints[k]}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </li>
          )
        })}
      </ol>
    </section>
  )
}

function ResultDetails({ result }: { result: AnalysisResult }) {
  return (
    <>
      <OrchestratorHistoryView result={result} />
      <h3>根本原因候補（{result.root_cause_candidates.length}）</h3>
      <ol className="candidates">
        {result.root_cause_candidates.map((c, i) => (
          <li key={i}>
            <span className={`badge cat-${c.category}`}>{c.category}</span>
            <span className="rank">rank {c.rank}</span>
            <div className="summary-text">{c.summary}</div>
            <details>
              <summary>evidence ({c.evidence.length})</summary>
              <ul className="evidence-list">
                {c.evidence.map((e, j) => <li key={j}><code>{e}</code></li>)}
              </ul>
            </details>
          </li>
        ))}
      </ol>

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

function ResultSummaryGrid({ result }: { result: AnalysisResult }) {
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
        <div className="summary-value mono small">{result.trace_id}</div>
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
  const [editorOpen, setEditorOpen] = useState<boolean>(false)
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
  const [rallyForceMinRounds, setRallyForceMinRounds] = useState<number>(0)

  const selectedConfigEntry = configList.find(c => c.id === selectedConfig)
  const selectedBaseConfig = selectedConfigEntry?.base_config ?? ''
  const isUserConfig = selectedConfigEntry?.type === 'user'
  const userConfigId = isUserConfig ? Number(selectedConfig.split(':')[1]) : null

  function getDefaultPrompt(slotId: string): string {
    return slots.find(s => s.slot_id === slotId)?.default_prompt ?? ''
  }

  function getDefaultModel(slotId: string): string {
    return slots.find(s => s.slot_id === slotId)?.default_model ?? ''
  }

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

  useEffect(() => {
    loadConfigs().then(configs => {
      if (configs.length > 0 && !selectedConfig) setSelectedConfig(configs[0].id)
      if (configs.length > 0) {
        const builtinIds = configs.filter(c => c.type === 'builtin').map(c => c.id)
        setCompareSelected(new Set(builtinIds))
      }
    })
    fetch(`${API_BASE}/api/logs`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then((d: { logs: LogEntry[] }) => {
        setLogs(d.logs)
        if (d.logs.length > 0) setSelectedLog(d.logs[0].name)
      })
      .catch(e => setError(`ログ一覧取得失敗: ${e.message}`))
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
        if (rallyForceMinRounds > 0) body.rally_force_min_rounds = rallyForceMinRounds
      }
      // user の場合は overrides は無視される（保存値が使われる）
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
        <button
          onClick={() => setMode('single')}
          className={mode === 'single' ? 'tab active' : 'tab'}
          disabled={singleRunning || isCompareRunning}
        >
          単一実行
        </button>
        <button
          onClick={() => setMode('compare')}
          className={mode === 'compare' ? 'tab active' : 'tab'}
          disabled={singleRunning || isCompareRunning}
        >
          構成比較
        </button>
        <button
          onClick={() => setMode('builder')}
          className={mode === 'builder' ? 'tab active' : 'tab'}
          disabled={singleRunning || isCompareRunning}
        >
          構成設計（pipeline）
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
              disabled={singleRunning || !selectedLog || !selectedConfig}
              className="run-button"
            >
              {singleRunning ? `実行中… ${singleElapsedSec}s` : '実行'}
            </button>
          </section>

          {selectedConfigEntry && selectedBaseConfig === 'config5' && (
            <div className="config5-hint">
              この構成は <strong>config5（user_pipeline）</strong> ベースです。プロンプトとモデルの編集は
              <strong>「構成設計（pipeline）」</strong>タブで行ってください。
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
                    max={6}
                    value={rallyMaxRounds}
                    onChange={e => setRallyMaxRounds(Math.max(1, Math.min(6, Number(e.target.value) || 1)))}
                    disabled={singleRunning}
                  />
                  <span className="rally-hint">orchestrator が回せる上限（1〜6）</span>
                </label>
                <label className="rally-control">
                  <span className="rally-label">強制最小ラウンド（PoC デモ用）</span>
                  <input
                    type="number"
                    min={0}
                    max={rallyMaxRounds}
                    value={rallyForceMinRounds}
                    onChange={e => setRallyForceMinRounds(Math.max(0, Math.min(rallyMaxRounds, Number(e.target.value) || 0)))}
                    disabled={singleRunning}
                  />
                  <span className="rally-hint">
                    {rallyForceMinRounds > 0
                      ? `${rallyForceMinRounds} 未満で finalize を選んでも override で再呼出を強制`
                      : '0=本番挙動（LLM 判断のまま）'}
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

          {singleResult && (
            <section className="result">
              <h2>結果</h2>
              <ResultSummaryGrid result={singleResult} />

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
