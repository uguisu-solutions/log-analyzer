import dagre from 'dagre'

/** dagre による階層レイアウト。React Flow の Node 配列に与える `position` を計算する。
 *
 * - サイクル構造（config4 の orchestrator ループ等）でも back-edge を除いた DAG として
 *   レイヤー化されるよう、内部で循環エッジを検出して除外する。
 * - ノードサイズは dagre が必要とする。React Flow 側で実描画サイズが違っても
 *   おおまかな位置決めには十分。
 */
export function layoutWithDagre(
  nodes: { id: string }[],
  edges: { source: string; target: string }[],
  options: {
    nodeWidth?: number
    nodeHeight?: number
    rankdir?: 'TB' | 'LR' | 'BT' | 'RL'
    nodesep?: number  // 同ランク内のノード間距離
    ranksep?: number  // ランク（深さ）間の距離
  } = {},
): Record<string, { x: number; y: number }> {
  const nodeWidth = options.nodeWidth ?? 200
  const nodeHeight = options.nodeHeight ?? 80
  const rankdir = options.rankdir ?? 'LR'
  const nodesep = options.nodesep ?? 60
  const ranksep = options.ranksep ?? 100

  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir, nodesep, ranksep, marginx: 20, marginy: 20 })

  nodes.forEach(n => {
    g.setNode(n.id, { width: nodeWidth, height: nodeHeight })
  })

  // back-edge を除外して DAG にする（dagre は cyclic にも一応耐えるが、レイアウト品質が下がる）
  const dag = removeBackEdges(nodes, edges)
  dag.forEach(e => {
    g.setEdge(e.source, e.target)
  })

  dagre.layout(g)

  const positions: Record<string, { x: number; y: number }> = {}
  nodes.forEach(n => {
    const node = g.node(n.id)
    if (node) {
      // dagre は中心座標を返すので、React Flow 用に左上座標へ変換
      positions[n.id] = { x: node.x - nodeWidth / 2, y: node.y - nodeHeight / 2 }
    }
  })
  return positions
}

function removeBackEdges<T extends { source: string; target: string }>(
  nodes: { id: string }[],
  edges: T[],
): T[] {
  const adj = new Map<string, string[]>()
  edges.forEach(e => {
    if (!adj.has(e.source)) adj.set(e.source, [])
    adj.get(e.source)!.push(e.target)
  })
  const visited = new Set<string>()
  const inStack = new Set<string>()
  const back = new Set<string>()
  function dfs(u: string) {
    visited.add(u)
    inStack.add(u)
    for (const v of adj.get(u) ?? []) {
      if (inStack.has(v)) back.add(`${u}->${v}`)
      else if (!visited.has(v)) dfs(v)
    }
    inStack.delete(u)
  }
  nodes.forEach(n => { if (!visited.has(n.id)) dfs(n.id) })
  return edges.filter(e => !back.has(`${e.source}->${e.target}`))
}
