/**
 * Terraform 一括取込モーダル (Phase E 拡張)。
 *
 * .tf ファイルアップロード or テキスト貼り付け → resource ブロックを抽出し、
 * ノード ID / extractedName / ラベル名と正規化比較してマッチング。
 * ユーザーが結果を確認した上で「適用」を押すと、マッチしたものを各ノードの
 * configs に追加する。既存の per-node アップロードと併存可能 (上書きしない)。
 */
import { useMemo, useState } from 'react'
import { matchResourcesToNodes, parseTerraform, type TfMatchResult } from './lib/terraformParser'
import type { NodeAttachments, TopologyNode } from './types'

interface Props {
  nodes: TopologyNode[]
  /** 適用時、{nodeId: 追加する NodeAttachment[]} を返すコールバック */
  onApply: (additions: NodeAttachments) => void
  onClose: () => void
}

export function TerraformImporter({ nodes, onApply, onClose }: Props) {
  const [content, setContent] = useState('')
  const [fileLabel, setFileLabel] = useState<string | null>(null)
  const [results, setResults] = useState<TfMatchResult[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // 手動上書き: resource index → node_id (null は除外)
  const [overrides, setOverrides] = useState<Record<number, string | null>>({})

  const handleFile = async (file: File) => {
    setError(null)
    if (file.size > 2 * 1024 * 1024) {
      setError('ファイルが大きすぎます (2MB 上限)')
      return
    }
    try {
      const text = await file.text()
      setContent(text)
      setFileLabel(file.name)
      setResults(null)
      setOverrides({})
    } catch (e) {
      setError(`ファイル読み込み失敗: ${(e as Error).message}`)
    }
  }

  const handleParse = () => {
    setError(null)
    if (!content.trim()) {
      setError('Terraform 内容が空です')
      return
    }
    try {
      const resources = parseTerraform(content)
      if (resources.length === 0) {
        setError('resource ブロックが見つかりませんでした')
        return
      }
      const matches = matchResourcesToNodes(resources, nodes)
      setResults(matches)
      setOverrides({})
    } catch (e) {
      setError(`解析失敗: ${(e as Error).message}`)
    }
  }

  // 各 resource の「最終的に割り当てるノード」を計算 (auto match → override 反映)
  const finalAssignments = useMemo<(string | null)[]>(() => {
    if (!results) return []
    return results.map((r, i) =>
      i in overrides ? overrides[i] : r.matchedNodeId
    )
  }, [results, overrides])

  const matchedCount = finalAssignments.filter(v => v).length
  const totalCount = results?.length ?? 0

  const handleApply = () => {
    if (!results) return
    const additions: NodeAttachments = {}
    results.forEach((r, i) => {
      const nodeId = finalAssignments[i]
      if (!nodeId) return
      const filename = `${r.resource.type}.${r.resource.label}.tf`
      if (!additions[nodeId]) additions[nodeId] = []
      additions[nodeId].push({ name: filename, content: r.resource.fullBlock })
    })
    onApply(additions)
    onClose()
  }

  const handleClear = () => {
    setContent('')
    setFileLabel(null)
    setResults(null)
    setOverrides({})
    setError(null)
  }

  return (
    <div className="modal-overlay">
      <div className="modal tf-importer-modal">
        <div className="tf-importer-header">
          <h3>Terraform 一括取込</h3>
          <button className="btn-close" onClick={onClose}>×</button>
        </div>
        <p className="modal-summary">
          <code>.tf</code> ファイルをアップロード or テキスト貼り付け → <code>resource "type" "label"</code> ブロックを抽出して
          各ノードの設定ファイルに自動割当します。ラベル / <code>name</code> 属性 / <code>tags.Name</code> を
          ノード id と正規化比較 (<code>_</code> ↔ <code>-</code>) してマッチング。
        </p>

        <div className="tf-importer-input">
          <div className="tf-importer-input-row">
            <label className="btn-file">
              ファイルを選択
              <input type="file" accept=".tf,.hcl,.txt" hidden
                onChange={async e => {
                  const f = e.target.files?.[0]
                  if (f) await handleFile(f)
                  e.target.value = ''
                }} />
            </label>
            {fileLabel && <span className="muted small">{fileLabel}</span>}
            <span className="muted small" style={{ marginLeft: 'auto' }}>
              またはテキスト貼り付け ↓
            </span>
          </div>
          <textarea
            className="tf-importer-textarea"
            value={content}
            onChange={e => { setContent(e.target.value); setFileLabel(null); setResults(null) }}
            placeholder='resource "aws_security_group" "fw_01" { ... }'
            rows={10}
          />
          <div className="tf-importer-actions">
            <button className="btn-secondary" onClick={handleClear} disabled={!content && !results}>
              クリア
            </button>
            <button className="run-button" onClick={handleParse} disabled={!content.trim()}>
              解析してプレビュー
            </button>
          </div>
        </div>

        {error && <div className="tf-importer-error">{error}</div>}

        {results && (
          <div className="tf-importer-preview">
            <h4>解析結果 ({totalCount} 件 / マッチ {matchedCount} 件)</h4>
            <table className="tf-importer-table">
              <thead>
                <tr>
                  <th>type</th>
                  <th>label</th>
                  <th>name / tags.Name</th>
                  <th>割当先ノード</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r, i) => {
                  const assigned = finalAssignments[i]
                  const matchedClass = assigned ? 'tf-row-matched' : 'tf-row-unmatched'
                  return (
                    <tr key={i} className={matchedClass}>
                      <td><code>{r.resource.type}</code></td>
                      <td><code>{r.resource.label}</code></td>
                      <td>{r.resource.extractedName ? <code>{r.resource.extractedName}</code> : <span className="muted small">-</span>}</td>
                      <td>
                        <select
                          value={assigned ?? ''}
                          onChange={e => setOverrides(prev => ({ ...prev, [i]: e.target.value || null }))}
                        >
                          <option value="">— 割当しない —</option>
                          {nodes.map(n => (
                            <option key={n.id} value={n.id}>
                              {n.id}{n.id === r.matchedNodeId ? ` (自動: ${r.matchedBy})` : ''}
                            </option>
                          ))}
                        </select>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="modal-actions">
          <button
            className="run-button"
            onClick={handleApply}
            disabled={!results || matchedCount === 0}
          >
            {matchedCount > 0 ? `${matchedCount} 件を各ノードに割当` : '割当先を選択してください'}
          </button>
          <button className="btn-secondary" onClick={onClose}>
            閉じる
          </button>
        </div>
      </div>
    </div>
  )
}
