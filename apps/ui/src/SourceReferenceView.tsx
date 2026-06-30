/**
 * 解析でソースコードがどう参照されたかを表示する（Phase 3）。
 *
 * - サマリ: コードベース / ファイル数 / シンボル数 / 取得文字数
 * - ノード別「参照したソース」: どの監視ノードが source_search / source_read / db_schema を
 *   どの対象に対して呼んだか
 * - DB スキーマ: テーブル・列（折りたたみ）
 */
import type { DbSchema, SourceContext, SourceToolCall } from './types'

const NODE_LABEL: Record<string, string> = {
  orchestrator: 'オーケストレータ',
  fw: 'FW 監視', routing: 'Routing 監視', app: 'App 監視',
  dns: 'DNS 監視', sec: 'Sec 監視', integrator: '統合',
}
function nodeLabel(n: string): string {
  return NODE_LABEL[n] ?? (n ? `${n} 監視` : '(不明)')
}

const TOOL_LABEL: Record<string, string> = {
  source_search: '検索', source_read: '取得', db_schema: 'DBスキーマ',
}

function callTarget(c: SourceToolCall): string {
  const a = c.args ?? {}
  if (c.tool === 'source_search') return `「${String(a.query ?? '')}」`
  if (c.tool === 'source_read') {
    const path = String(a.path ?? '')
    const sym = a.symbol ? `:${String(a.symbol)}` : ''
    return `${path}${sym}`
  }
  if (c.tool === 'db_schema') return a.table ? String(a.table) : '(全テーブル)'
  return ''
}

function DbSchemaBlock({ schema }: { schema: DbSchema }) {
  if (!schema.tables || schema.tables.length === 0) return null
  return (
    <details className="source-db-schema" style={{ marginTop: 8 }}>
      <summary>DB スキーマ（{schema.tables.length} テーブル）</summary>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 6 }}>
        {schema.tables.map(t => (
          <div key={t.name} className="summary-card" style={{ minWidth: 220, alignItems: 'flex-start' }}>
            <div className="summary-label">
              {t.name} <span className="muted small">[{t.sources.join('+') || '?'}]</span>
            </div>
            <ul className="small mono" style={{ margin: '4px 0 0', paddingLeft: 16 }}>
              {t.columns.map(col => (
                <li key={col.name}>
                  {col.name} {col.type}
                  {col.primary_key && ' 🔑'}
                  {!col.nullable && !col.primary_key && ' ·NN'}
                  {col.foreign_key && ` →${col.foreign_key}`}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </details>
  )
}

export function SourceReferenceView({ context }: { context: SourceContext }) {
  const calls = context.tool_calls ?? []
  // ノード別にグルーピング（登場順を保つ）
  const order: string[] = []
  const byNode = new Map<string, SourceToolCall[]>()
  for (const c of calls) {
    const key = c.node || '(不明)'
    if (!byNode.has(key)) { byNode.set(key, []); order.push(key) }
    byNode.get(key)!.push(c)
  }

  return (
    <div className="source-reference panel-block">
      <h4>参照したソースコード</h4>
      <div className="muted small" style={{ marginBottom: 8 }}>
        コードベース <strong>{context.codebase || '—'}</strong> ·
        {' '}{context.file_count} ファイル · {context.symbol_count} シンボル ·
        {' '}取得 {context.total_chars_fetched.toLocaleString()} 文字 / {calls.length} 回
      </div>

      {order.length === 0 ? (
        <div className="muted small">（このコードベースはツールで参照されませんでした）</div>
      ) : (
        <ul className="source-ref-list" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
          {order.map(node => (
            <li key={node} style={{ marginBottom: 6 }}>
              <strong>{nodeLabel(node)}</strong>
              <ul className="small" style={{ margin: '2px 0 0', paddingLeft: 18 }}>
                {byNode.get(node)!.map((c, i) => (
                  <li key={i}>
                    <span className="muted">[r{c.round} {TOOL_LABEL[c.tool] ?? c.tool}]</span>{' '}
                    <span className="mono">{callTarget(c)}</span>
                    {c.result_chars > 0 && <span className="muted small"> （{c.result_chars.toLocaleString()} 文字）</span>}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}

      {context.db_schema && <DbSchemaBlock schema={context.db_schema} />}
    </div>
  )
}
