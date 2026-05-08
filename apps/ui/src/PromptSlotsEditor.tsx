import { useState } from 'react'
import type { SlotInfo } from './types'

interface Props {
  slots: SlotInfo[]
  promptOverrides: Record<string, string>
  modelOverrides: Record<string, string>
  onPromptChange: (slotId: string, value: string) => void
  onModelChange: (slotId: string, value: string) => void
  disabled?: boolean
}

export function PromptSlotsEditor({
  slots,
  promptOverrides,
  modelOverrides,
  onPromptChange,
  onModelChange,
  disabled,
}: Props) {
  // 各 slot の開閉状態を local state で管理。初期値は「上書き有り = 開く」。
  // 親から ``key`` が変わるとコンポーネントが再マウントされて初期化される（構成切替に追従）。
  const [openSlots, setOpenSlots] = useState<Record<string, boolean>>(() => {
    const init: Record<string, boolean> = {}
    for (const slot of slots) {
      const promptModified =
        slot.slot_id in promptOverrides && promptOverrides[slot.slot_id] !== slot.default_prompt
      const modelModified =
        slot.slot_id in modelOverrides && modelOverrides[slot.slot_id] !== slot.default_model
      if (promptModified || modelModified) init[slot.slot_id] = true
    }
    return init
  })

  if (slots.length === 0) {
    return <div className="slots-empty">slot 情報を取得中…</div>
  }

  return (
    <div className="slots-editor">
      {slots.map(slot => {
        const promptValue = promptOverrides[slot.slot_id] ?? slot.default_prompt
        const promptModified =
          slot.slot_id in promptOverrides && promptOverrides[slot.slot_id] !== slot.default_prompt
        const modelValue = modelOverrides[slot.slot_id] ?? slot.default_model
        const modelModified =
          slot.slot_id in modelOverrides && modelOverrides[slot.slot_id] !== slot.default_model
        const modified = promptModified || modelModified
        const lines = promptValue.split('\n').length
        const isOpen = openSlots[slot.slot_id] ?? false
        const modelOverridable = slot.allowed_models.length > 0
        return (
          <details
            key={slot.slot_id}
            className="slot"
            open={isOpen}
            onToggle={e => {
              const next = (e.target as HTMLDetailsElement).open
              setOpenSlots(prev => ({ ...prev, [slot.slot_id]: next }))
            }}
          >
            <summary>
              <span className="slot-label">{slot.label}</span>
              {modified && <span className="modified-badge">変更あり</span>}
              <span className="slot-id">{slot.slot_id}</span>
            </summary>
            <div className="slot-body">
              <div className="slot-model-row">
                <label className="slot-model-label">モデル</label>
                {modelOverridable ? (
                  <select
                    value={modelValue}
                    onChange={e => onModelChange(slot.slot_id, e.target.value)}
                    disabled={disabled}
                    className="slot-model-select"
                  >
                    {slot.allowed_models.map(m => (
                      <option key={m} value={m}>
                        {m}
                        {m === slot.default_model ? ' (既定)' : ''}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="slot-model-fixed">{slot.default_model}（固定）</span>
                )}
              </div>
              <textarea
                value={promptValue}
                onChange={e => onPromptChange(slot.slot_id, e.target.value)}
                rows={Math.min(24, Math.max(8, lines))}
                className="slot-textarea"
                spellCheck={false}
                disabled={disabled}
              />
            </div>
          </details>
        )
      })}
    </div>
  )
}
