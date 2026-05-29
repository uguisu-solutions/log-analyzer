/**
 * 問診票パネル (Phase B)。
 *
 * トポロジー解析タブと Config-First 解析タブの両方で再利用可能なフォーム。
 * テンプレートを GET /api/questionnaires から読み込み、ユーザーが回答を埋め、
 * 親コンポーネントに `{key: answer}` の辞書として渡す。
 *
 * 設計判断:
 * - 折りたたみ可能（既定: 折りたたみ）。問診票は任意入力なので「邪魔にならない」を優先
 * - テンプレ切替時に既存回答はクリア（key が変わる可能性があるため）
 * - 「テンプレを編集」リンクは現状なし（CRUD は API のみ提供、UI は将来追加）
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { QuestionnaireAnswers, QuestionnaireItem, QuestionnaireTemplate } from './types'

const API_BASE = 'http://localhost:8000'

interface Props {
  answers: QuestionnaireAnswers
  onAnswersChange: (next: QuestionnaireAnswers) => void
  disabled?: boolean
}

export function QuestionnairePanel({ answers, onAnswersChange, disabled }: Props) {
  const [templates, setTemplates] = useState<QuestionnaireTemplate[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [expanded, setExpanded] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)

  const loadTemplates = useCallback(() => {
    fetch(`${API_BASE}/api/questionnaires`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then((d: { templates: QuestionnaireTemplate[] }) => {
        setTemplates(d.templates)
        // 既定の選択: name='default' があればそれ、なければ先頭
        if (selectedId == null && d.templates.length > 0) {
          const def = d.templates.find(t => t.name === 'default') ?? d.templates[0]
          setSelectedId(def.id)
        }
      })
      .catch(e => setError(`問診票テンプレ取得失敗: ${e.message}`))
  }, [selectedId])

  useEffect(() => {
    loadTemplates()
  }, [loadTemplates])

  const selected = useMemo(
    () => templates.find(t => t.id === selectedId) ?? null,
    [templates, selectedId],
  )

  const items: QuestionnaireItem[] = selected?.items ?? []
  const answeredCount = items.reduce((n, it) => (answers[it.key]?.trim() ? n + 1 : n), 0)

  const handleTemplateChange = (id: number) => {
    setSelectedId(id)
    // 回答は一旦クリア (項目 key が変わる可能性があるため誤マッピングを防ぐ)
    onAnswersChange({})
  }
  const handleItemChange = (key: string, value: string) => {
    const next = { ...answers, [key]: value }
    if (!value.trim()) delete next[key]
    onAnswersChange(next)
  }

  return (
    <details className="questionnaire-panel" open={expanded} onToggle={e => setExpanded((e.target as HTMLDetailsElement).open)}>
      <summary>
        <span className="qp-title">問診票</span>
        <span className="qp-meta">
          {selected ? `テンプレ: ${selected.name}` : '読み込み中...'}
          {items.length > 0 && (
            <span className="qp-count">  回答済 {answeredCount}/{items.length}</span>
          )}
        </span>
        <span className="qp-hint muted">クリックで開閉。回答は任意 (空でも実行可)</span>
      </summary>
      {error && <div className="qp-error">{error}</div>}
      <div className="qp-controls">
        <label className="qp-template-select">
          <span>テンプレート:</span>
          <select
            value={selectedId ?? ''}
            onChange={e => handleTemplateChange(Number(e.target.value))}
            disabled={disabled || templates.length === 0}
          >
            {templates.map(t => (
              <option key={t.id} value={t.id}>{t.name}{t.description ? ` — ${t.description}` : ''}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="btn-secondary btn-small"
          onClick={() => onAnswersChange({})}
          disabled={disabled || answeredCount === 0}
        >
          回答をクリア
        </button>
      </div>
      <div className="qp-items">
        {items.map(it => (
          <QPItem key={it.key} item={it} value={answers[it.key] ?? ''} onChange={(v) => handleItemChange(it.key, v)} disabled={!!disabled} />
        ))}
        {items.length === 0 && <div className="qp-empty">（テンプレに設問が定義されていません）</div>}
      </div>
    </details>
  )
}

interface QPItemProps {
  item: QuestionnaireItem
  value: string
  onChange: (v: string) => void
  disabled: boolean
}
function QPItem({ item, value, onChange, disabled }: QPItemProps) {
  return (
    <div className="qp-item">
      <label className="qp-item-label">
        <span>{item.label}{item.required && <em className="qp-required">必須</em>}</span>
      </label>
      {item.type === 'choice' ? (
        <select value={value} onChange={e => onChange(e.target.value)} disabled={disabled}>
          <option value="">(未選択)</option>
          {item.options.map(o => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : item.type === 'textarea' ? (
        <textarea
          value={value} rows={3}
          placeholder={item.placeholder}
          onChange={e => onChange(e.target.value)}
          disabled={disabled}
        />
      ) : (
        <input
          type="text" value={value}
          placeholder={item.placeholder}
          onChange={e => onChange(e.target.value)}
          disabled={disabled}
        />
      )}
    </div>
  )
}
