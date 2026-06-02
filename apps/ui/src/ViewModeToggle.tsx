/**
 * 結果ペインの表示モード切替 (Phase E)。
 *
 * 「標準」(従来のセクション分割表示) と「チャット」(会話スレッド表示) を切り替える。
 * TopologyAnalysis / ConfigLogAnalysis 両タブで共用。
 */
interface Props {
  mode: 'standard' | 'chat'
  onChange: (m: 'standard' | 'chat') => void
}

export function ViewModeToggle({ mode, onChange }: Props) {
  return (
    <div className="view-mode-toggle">
      <span className="view-mode-label">表示モード:</span>
      <label className="radio-pill">
        <input type="radio" name="view-mode" checked={mode === 'standard'} onChange={() => onChange('standard')} />
        <span>標準</span>
      </label>
      <label className="radio-pill">
        <input type="radio" name="view-mode" checked={mode === 'chat'} onChange={() => onChange('chat')} />
        <span>チャット表示</span>
      </label>
    </div>
  )
}
