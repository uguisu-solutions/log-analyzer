# ログ駆動マルチエージェント検証プラットフォーム 実装計画

最終更新: 2026-05-07
出典: `docs/preset/IBC_PoC実行計画_社員向け.pptx` / `docs/preset/ログ駆動マルチエージェント検証基盤_構築提案_v3.pptx`

---

## 0. ドキュメントの位置づけ

本ドキュメントは、上記2つのプレゼン資料（PoC実行計画／技術選定提案v3）を統合した、**実装着手のための単一の指示書**である。

- **PoC実行計画（18枚）** … IBC案件の進め方（4構成・12週・役割・評価方法）の業務側プラン。
- **技術選定提案v3（35枚）** … 5レイヤ統合アーキテクチャの技術根拠とツール選定の技術側プラン。

本ドキュメントは両者を突き合わせ、「**何を / いつ / どの技術で / 誰が**」作るかを具体化する。エンジニアはこのファイルだけを起点に着手できることを目標とする。

---

## 1. プラットフォームの目的と一次ゴール

### 1.1 一次ゴール（12週で達成）

> 4種類のエージェント構成を切り替えて精度・コスト・運用負荷を比較できるプラットフォームを構築し、IBCが本番展開時に何を採るべきかの判断材料を提供する。

### 1.2 検証で答えるべき5つの問い

| # | 問い |
|---|---|
| Q1 | 単純LLMでどこまで答えられるか |
| Q2 | 前処理は精度・コストにどう効くか |
| Q3 | マルチモデルは単一より優れるか |
| Q4 | ラリー型は問題種別ごとに最適化できるか |
| Q5 | 管理UIで試行錯誤が回るか |

### 1.3 残すアセット

- IBC社内で **構成を試行錯誤し続けられる体制**（管理UI＋共通スキーマ＋評価データセット）
- 4構成の比較レポート（精度・コスト・運用負荷）
- IBC運用への移管手順書

---

## 2. アーキテクチャ：5レイヤ統合構成

技術提案v3で確定した構成A（Production-Ready）をベースに、AWS 不使用方針（2026-05-07 決定）を反映した PoC 構成を採用する。各レイヤは独立に選定し、AgentOps層が全体を横断観測する。

| レイヤ | 役割 | 採用ツール | サブ候補（縮退時） |
|---|---|---|---|
| GUI / Visual Builder | 管理UI・Prompt編集・Multi-model比較 | **Dify** | Flowise / Langflow |
| Agent Framework | Multi-Agent Orchestration | **LangGraph** | CrewAI |
| AgentOps / Eval | Trace・精度評価・コスト・Prompt版管理 | **Langfuse**（OSS） | LangSmith（本番化時に切替） |
| Visualization | Agent組織図 | **React Flow** | （代替なし／必須） |
| Runtime | 並列・非同期実行 | **Python `asyncio` + LangGraph**（in-process） | AWS が確保できれば Step Functions に切替 |
| Foundation Models | LLM 推論 | **Anthropic API 直叩き**（Sonnet 4.5 / Haiku 4.5） + **OpenAI API**（GPT-4o、構成3 の 3rd モデル） | AWS Bedrock 経由（将来） |
| Storage | 結果・構成・トレース永続化 | **SQLite**（`apps/agents/data/results.sqlite3`、W5 以降に導入） + ローカル FS（`samples/logs/`, `configs/`） | DynamoDB / S3（将来） |

### 2.1 採用根拠サマリ

- **LangGraph**: Multi-Agent / 状態管理 / Orchestration / 並列実行の4軸で◎。構成4ラリー型を実現できる唯一の候補（13/15点）。in-process Python ランタイムで動くため AWS 非依存。
- **Dify**: Prompt IDE・Multi-model比較・RAG・API公開を1製品でカバー（15/15点）。要件1・3・8を最も忠実に満たす。
- **Langfuse（PoC）→ LangSmith（本番）**: Phase 1 では OSS の Langfuse を採用。Trace・Eval・Cost・Prompt版管理の本番運用4軸で必要十分。本番運用時は LangSmith への切替を検討（二段構え戦略）。
- **React Flow**: ノード/エッジ完全カスタム可・MIT・商用実績豊富。Agent組織図の独自描画に必須。
- **Python asyncio + LangGraph（Runtime）**: AWS Step Functions が使えないため、構成3 の並列実行は `asyncio.gather`、構成4 のラリー型は LangGraph の状態機械機能で実現する。長時間実行・Durable execution は本 PoC の評価範囲外（数十秒〜数分のシナリオに限定）。
- **OpenAI GPT-4o（3rd モデル）**: Amazon Nova の代替。Anthropic 系列とは独立したベンダーなので Q3「マルチモデルは単一より優れるか」の問いに有意な答えを得られる。

### 2.2 全体図

```
┌──────────────────────────────────────────────────────────┐
│ UI Layer                                                  │
│   ┌────────────────┐   ┌──────────────────────┐          │
│   │ Dify           │   │ React Flow Canvas    │          │
│   │ (Prompt/Model) │   │ (Agent組織図)         │          │
│   └────────────────┘   └──────────────────────┘          │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼ CLI / 将来は HTTP API
┌──────────────────────────────────────────────────────────┐
│ Runtime: Python (in-process)                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐    │
│  │ 構成1     │ │ 構成2     │ │ 構成3     │ │ 構成4     │    │
│  │ 単純LLM   │ │ 前処理あり │ │ Multi-Mdl │ │ ラリー型  │    │
│  │ baseline │ │ filter→LLM│ │(asyncio.  │ │(LangGraph │    │
│  │  _agent  │ │  _agent  │ │  gather)  │ │ stategraph│    │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘    │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│ Foundation Models                                         │
│   Anthropic API: Claude Sonnet 4.5 / Claude Haiku 4.5    │
│   OpenAI API:    GPT-4o（構成3 の 3rd モデル）            │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼ 共通出力JSONスキーマ
┌──────────────────────────────────────────────────────────┐
│ Storage:                                                  │
│   SQLite (results / configs、W5 以降に導入)                │
│   ローカル FS (samples/logs/, configs/)                   │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼ Trace連携
┌──────────────────────────────────────────────────────────┐
│ AgentOps: Langfuse （横断観測）                            │
│   Trace / Eval / Cost / Latency / Prompt Versioning      │
└──────────────────────────────────────────────────────────┘
```

> 図中の AWS マネージドサービス（API Gateway / SQS / Lambda / Step Functions / Bedrock / DynamoDB / S3 / LangSmith SaaS）は AWS 不使用方針（2026-05-07 決定）に伴い、すべて in-process Python / Anthropic & OpenAI API / SQLite & ローカル FS / Langfuse OSS に置換した。本番化フェーズで AWS が確保できればレイヤごとに段階的に移管できる構造を保つ。

---

## 3. 4つの検証構成

すべての構成は**共通の出力JSONスキーマ**を返す。これが機械的な比較を可能にする最重要規約である。

### 3.1 共通出力スキーマ v0.1（W1で確定）

```json
{
  "trace_id": "uuid",
  "config_id": "config1 | config2 | config3 | config4",
  "input_log_ref": "s3://...",
  "root_cause_candidates": [
    { "rank": 1, "category": "FW|Net|App|DNS|Sec", "summary": "...", "evidence": ["log line N"] }
  ],
  "recommended_actions": [
    { "action": "...", "human_judgment_required": true|false, "risk_level": "low|mid|high" }
  ],
  "confidence": 0.0,
  "agent_trace": [ /* ノード呼び出し履歴ツリー */ ],
  "metrics": {
    "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
    "latency_ms_p50": 0, "latency_ms_total": 0,
    "compression_ratio": 0.0
  },
  "info_loss_flags": ["..."]
}
```

> `human_judgment_required` フラグは UI 上で **外せない仕組み**にする（議事録L3：ロールバック・リストアの自動実行は危険）。

### 3.2 構成1：単純LLM（W2／5人日／AIエンジニアサブ）

ベースライン。すべての比較基準。

1. ログを `s3://ibc-poc/logs/` にアップロード（管理UI or 直接）
2. API Gateway → 受付Lambda → SQS → 実行Lambda
3. Bedrock Converse API（Claude Sonnet 4.5）にログ全文＋システムプロンプト送信。**200KB上限**
4. 共通出力スキーマで返す
5. DynamoDB `trace_events` テーブルにトークン・レイテンシ記録

### 3.3 構成2：フィルタ済み（W3／7人日／AIエンジニアサブ）

前処理の効果測定。大規模ログ対応の本命。

1. **ルールベースフィルタ**: ERROR/WARN行抽出、KEEPALIVE等の正常パターンは件数のみ残す（Python正規表現）
2. **Haiku 4.5で構造化サマリ生成**: TriageCard相当を安価に生成。元ログ20%以下に圧縮
3. 圧縮後ログ＋元ログの異常行を**Sonnet 4.5**に投入
4. 構成1と同じ出力フォーマット
5. **圧縮率・情報欠損率**をDynamoDBに記録（明星氏指摘の重要情報がどれだけ残ったか）

### 3.4 構成3：マルチモデル（W4／8人日／AIエンジニアリード）

複数LLMで並列実行→統合。3rd モデルは AWS 不使用方針により Amazon Nova の代わりに **OpenAI GPT-4o** を採用（2026-05-07 決定）。

```
ログ ──┬─→ Claude Sonnet 4.5 ──┐
       ├─→ Claude Haiku 4.5  ──┼─→ 統合エージェント (Sonnet 4.5) ─→ 最終結論
       └─→ OpenAI GPT-4o     ──┘
```

1. **Python `asyncio.gather`** で3モデル同時起動。各モデルは独立に共通スキーマで返す
2. 統合エージェント（Sonnet 4.5）が**多数決＋整合性チェック**で最終結論
3. **各モデル単独の結果も保存**（後でモデル別精度を分析。Langfuse trace に親子関係で記録）
4. コストはほぼ3倍。精度向上幅と見合うかを評価でジャッジ
5. ベンダー独立性: Anthropic 系列（Sonnet/Haiku）と OpenAI 系列（GPT-4o）を混在させることで、Q3「マルチモデルは単一より優れるか」を**ベンダー横断**で評価できる

### 3.5 構成4：ラリー型（W7／12人日／AIエンジニアリード／LangGraph採用）

本命候補。オーケストレータが分岐判断。

```
                  ┌────────────────────────────┐
                  │ オーケストレータ           │
                  │ （LangGraph）              │
                  └────────────────────────────┘
                              │ 判断
        ┌────────┬─────────┬──┴──┬─────────┬────────┐
        ▼        ▼         ▼     ▼         ▼        ▼
       FW    ルーティング  アプリ  DNS    セキュリティ  …
       監視     監視       層監視  監視      監視
        └──────────────── 統合・出力 ─────────────────┘
```

オーケストレータの判断ロジック：

| 状況 | 動作 |
|---|---|
| FW関連ログのみ多数 | FW監視を単独起動 |
| FWとルーティング両方の異常 | 両者を並列起動 → 統合 |
| FW監視が「上流問題の可能性」を返した | ルーティング監視を**追加起動（ラリー2回目）** |
| どの領域か判定不能 | 全監視エージェント並列起動 → 確度高い結果採用 |

**監視エージェントは `read_topology` / `get_config` ツールを持つ**（議事録L2：構成図・コンフィグも参照する設計）。

> **2026-05-14 設計変更**: 上記の「並列ファンアウト + 再評価ループ」型は廃止し、
> **シングルアクティブな委譲チェーン**に置き換えた。オーケストレータは初回 1 回のみ
> 動作し最初の監視を 1 つ選ぶ。以降は各監視自身が分析後に次ノード（別監視 or
> integrator）を 1 つだけ指名する。自己遷移と直前ノードへの即時 ping-pong は
> 禁止（違反時は integrator にフォールバック）。上限到達時は SSE で UI に
> 確認モーダルを表示し、ユーザーが延長 / 停止を選ぶ。
> LangGraph 依存は撤去し、手動 async ループ + `StreamingResponse` で実装。
> 詳細は [docs/reports/poc_progress_2026-05-14.md](../reports/poc_progress_2026-05-14.md) 参照。

> **2026-05-25 追加**: 構成4 を **「ネットワーク構成図 + ノード別ログ・設定ファイル」入力モード**
> でも呼び出せるよう、UI 側に **トポロジー解析タブ** と backend に
> `POST /api/runs/topology-stream` を追加。topology + 各ノードに添付された
> **複数のログファイル + 複数の設定ファイル (Config)** を 1 本のログ文字列に合成して
> 既存の `run_rally_stream` に流し、integrator が `suspected_nodes: [{node_id, summary, severity}]`
> を出力する（schema 拡張: `AnalysisResult.suspected_node_ids` + `suspected_node_findings`、
> schema_version は v0.1 据置）。UI は severity 別に矩形を配色（primary=赤+点滅 /
> secondary=橙 / info=ハイライトなし）。委譲チェーンや確認モーダルなど構成4 既存資産は
> そのまま流用。詳細は [docs/reports/poc_progress_2026-05-25.md](../reports/poc_progress_2026-05-25.md) 参照。

### 3.6 構成カバレッジ表（Dify単体 vs LangGraph必須）

| 機能 | Dify単体 | LangGraph必須 |
|---|:---:|:---:|
| Prompt編集 / 単純Workflow / RAG / Agent UI | ○ | × |
| 構成比較 / Multi-agent ラリー / 状態管理 / 複雑分岐 | △ | ○ |
| 長時間実行 / Durable execution | × | ○ |

→ **構成1・2はDifyで管理／構成3・4はLangGraph実装が必須**。

---

## 4. 管理UI（エージェント組織図）

上司案の核。React Flowでノードベースに作る。**フロントエンドエンジニア新規アサイン要**。

### 4.1 主要機能

| 機能 | 説明 |
|---|---|
| ノードD&D | エージェントを画面に配置、線で親子関係 |
| プロンプト編集 | 選択ノードのプロンプトをモーダルで編集（バージョン管理付き） |
| 構成保存 | YAML形式でDynamoDBに保存・呼び出し |
| 検証実行 | ログをアップロード→構成を実行→結果表示 |
| 構成比較 | 複数構成の結果を横並びで精度比較 |
| 人間判断必須フラグ | UI 上で**外せない**仕組み（議事録L3） |
| **トポロジー解析タブ** | 2026-05-25 追加。ネットワーク構成図画像 + 各ノードの複数ログ + 複数設定ファイル (Config) を取り込み、構成4 (rally) で解析し、障害ノードを severity 別 (直接原因 / 影響を受けた側 / 参考) に矩形ハイライト |

### 4.2 検証パイプラインIF

```
[ログ選択] → [構成選択] → [実行] → [結果表示] → [他構成と比較]
```

| ステージ | 中身 |
|---|---|
| ログ選択 | S3パス指定 or ファイルアップロード（数MBまで） |
| 構成選択 | 保存済み構成からプルダウン。新規作成も可 |
| 実行 | API → SQS → 該当構成のLambda起動。リアルタイム進捗表示 |
| 結果表示 | 共通出力JSON + agent_traceツリー + ログ参照リンク |
| 比較 | 複数実行を横並び。精度・コスト・レイテンシの差分ハイライト |

> **コードを触らずに構成変更→検証→比較が回せる** ことがプラットフォーム性の核。

### 4.3 UI 実装方針

- **React Flowテンプレート活用**、PoC品質に徹する
- マルチユーザ・SSO・監査ログは**本番化フェーズへ後送り**
- 工数削減のため、Dify標準UIで賄えるもの（Prompt編集・Model切替）は Dify を埋め込み or リンク。React Flow Canvas は**Agent組織図と構成保存**に専念

---

## 5. 12週ロードマップ

| 週 | フェーズ | 主要アクティビティ | 成果物 |
|---|---|---|---|
| W1 | 土台＋IBC合意 | v2.0方針説明・合意取得 / 費用追加交渉 / フロントエンド確保 / Bedrock有効化 / 共通スキーマv0.1 / 監視Agent候補リスト / S3バケット設計 / 構成1スケルトン / 10シナリオ仮設定確認 | 合意済み計画 / スキーマv0.1 / AWS環境 |
| W2 | 構成1実装 | Lambda + Bedrock Converse + DynamoDB保存 + 動作確認 | **構成1動作** |
| W3 | 構成2実装＋実ログ | Pythonフィルタ / Haiku圧縮 / Sonnet推論 / 圧縮率記録 | **構成2動作 / 実ログ通過** |
| W4 | 構成3実装 | Step Functions Parallel / 統合エージェント / モデル別保存 | **構成3動作（MS1）** |
| W5 | 管理UI実装 | React Flow Canvas / プロンプト編集モーダル / YAML保存 | UI骨格 |
| W6 | 管理UI実装 | 検証実行画面 / 結果表示 / 構成比較ビュー / API連携 | **管理UI完成** |
| W7 | 構成4実装 | LangGraph orchestrator / 5監視Agent / read_topology・get_config ツール / ラリー2回目 | **構成4動作** |
| W8 | 全構成統合 | 4構成すべてが管理UIから実行可能 / agent_trace可視化 / LangSmith連携 | **MS2: 4構成統合** |
| W9 | 10シナリオ評価 | 10シナリオ × 4構成 = 40実行 / 機械突合 | 評価データ |
| W10 | 比較分析＋改善 | 明星氏ブラインドレビュー / 構成別比較表作成 | **MS3: 評価完了** |
| W11 | 総括 | レポートドラフト / 縮小オプション反映 | レポートβ版 |
| W12 | IBC移管 | 運用手順書 / 引継ぎセッション / 最終レポート | **MS4: 移管完了** |

### マイルストン

| ID | 時期 | 達成基準 |
|---|---|---|
| MS1 | W4末 | 構成1〜3が動作＋実ログを構成2で通過 |
| MS2 | W8末 | 管理UI完成＋構成4が動作＋4構成すべて統合 |
| MS3 | W10末 | 10シナリオ × 4構成の評価完了＋比較表完成 |
| MS4 | W12末 | IBCへの移管完了 |

---

## 6. 役割分担

| 役割 | 工数 | 担当領域 |
|---|---|---|
| PM（綾佳） | 週8h × 12週 | IBC折衝、上司報告、進捗管理、最終レポート |
| AIエンジニア（リード） | 週24h × 12週 | 構成3・構成4実装、オーケストレータ、評価設計 |
| AIエンジニア（サブ） | 週20h × 12週 | 構成1・構成2実装、ツール群、フィルタ・圧縮処理 |
| フロントエンド（**新規アサイン**） | 週24h × 6週（W4-W9） | 管理UI実装（React Flow）、API連携、比較画面 |
| インフラ（スポット） | 計30h | Langfuse Docker 運用、SQLite スキーマ設計、IBC移管 |
| 明星氏（IBC） | 計8h | 10シナリオ事前回答、ブラインドレビュー |

> AWS 不使用方針（2026-05-07）に伴い、当初想定の AWS CDK / Bedrock / Step Functions の構築工数が消滅し、インフラ役割は計60h → 計30h に縮減できる。

> **フロントエンドエンジニアの新規アサインは、PMのW1最優先タスク**。確保できないと W5以降の管理UIが崩壊する。

---

## 7. 評価方法

### 7.1 評価マトリクス：4構成 × 10シナリオ

| 軸 | 指標 |
|---|---|
| 精度 | 明星基準と原因候補1位の一致率 / False Positive率 / False Negative率 |
| コスト・速度 | 1シナリオあたりトークン消費 / 推定コスト($) / レイテンシ P50・P90・P99 |
| 運用負荷 | 管理UIで構成変更にかかるステップ数 / 構成切替の所要時間 / agent_trace完全性 |

### 7.2 評価実行手順（W9-W10）

1. 10シナリオ × 4構成 = **40実行を全件回す**
2. 共通出力スキーマで保存
3. 明星基準と機械突合（DynamoDB集計）
4. **明星氏ブラインドレビュー**
5. 構成別の精度・コスト・運用負荷を比較表に

### 7.3 KPI（提案v3 期待効果より）

| KPI | 目標 |
|---|---|
| 改善サイクル時間 | 実装→検証で日次 → **1時間以内** |
| 計測カバレッジ | **全実行の100%（Trace粒度）** |
| 障害特定時間 | 従来比 **▲50%** |
| PoC資産再利用率 | Promptは100% / Workflowは70%以上 |

---

## 8. リスクとスコープ縮小の発動順

予算・期間が圧迫されたら、**この順番で**削る。

### 主要リスク

| ID | リスク |
|---|---|
| R1 | IBC合意取得が遅延 or 拒否 |
| R2 | 管理UI実装が想定より重い |
| R3 | 4構成の精度に差が出ない |
| R4 | オーケストレータ判断が低品質 |
| R5 | 予算交渉が通らない |
| R6 | 明星氏稼働が確保できない |

### 縮小発動順

1. 構成3を3モデル → 2モデルに縮小
2. 管理UI比較画面を簡略化
3. IBC実ログ実証を3件 → 1件に縮小
4. 構成2のフィルタを省略（Haiku圧縮のみ）
5. 管理UIの構成保存数を5に制限
6. **構成3を完全省略（最後の最後）**

> 構成4（ラリー型）は本命候補なので、**構成3を捨てても4は守る**順序。

---

## 9. 議事録L1〜L4：上司案で抜けている観点の補強

| ID | 観点 | 議事録該当 | 本計画での対応 |
|---|---|---|---|
| L1 | シニア暗黙知の構造化 | 5.3 | 10シナリオの**正解データを明星氏に作成依頼**。RAG投入は最小限に縮小して継続 |
| L2 | 構成図・コンフィグも見る | 2.1 | **構成2の前処理 と 構成4の監視Agent** が `read_topology` / `get_config` ツールを持つ |
| L3 | 自動実行の危険 | 4 | 管理UIに **`human_judgment_required` フラグを必須化**、外せない仕組み |
| L4 | 8週では収まらない | — | **12週への後ろ倒しを前提化**（v2.0で根拠提示済み） |

---

## 10. W1（来週）の具体タスク

PoC開始の最初の1週間で全員が動くべきこと。

### PM（綾佳）
- IBC側に v2.0方針説明・**合意取得**
- 上司に費用追加交渉（650 → 970万円）
- **フロントエンドエンジニア確保**（最優先）

### AIエンジニア（リード）
- AWS環境準備、Bedrock有効化
- **共通出力スキーマ v0.1 ドラフト**
- 監視エージェント候補リストアップ

### AIエンジニア（サブ）
- IBCログ種別の確認・整理
- S3バケット設計
- **構成1のスケルトン作成**

### 明星氏（IBC）
- 10シナリオの仮設定確認
- 事前回答フォーマットのレビュー
- 実ログのサニタイズ方針確認

### W1末レビュー
- **日時: 2026-04-30（金）15:00**
- 確認項目: IBC合意 + 環境準備完了 + スキーマv0.1

---

## 11. 段階的導入（Phase 1 → Phase 3）

技術提案v3のフェーズ移行と本PoCロードマップを対応付ける。

| Phase | ゴール | 対象レイヤ | 完了基準 | 本PoC対応週 |
|---|---|---|---|---|
| Phase 1 立ち上げ | GUIで触れる検証環境を最短で立ち上げる | GUI: Dify / Framework: Anthropic SDK 直叩き / Ops: Langfuse | Promptが編集できる / Multi-modelが横並びで比較できる / 簡単なAgent動作のTraceが取れる | W1-W4 |
| Phase 2 拡張 | ラリー型Multi-Agent・SQLite永続化を可能にする | Framework: **LangGraph 追加**（構成4 用） / Visualization: **React Flow 追加** / Runtime: **Python asyncio**（構成3 並列） / Storage: **SQLite 追加** | 構成3/4が動作する / Agent組織図が描ける / 並列処理ができる / 結果が SQLite に永続化される | W5-W8 |
| Phase 3 本番運用化 | 監査・SLO・コスト最適化を含めた運用体制を確立 | Ops: 必要に応じて **LangSmith 置換** / 監査ログ整備 / Cost・Latency SLO定義 / **AWS が確保できれば Step Functions / Bedrock / DynamoDB に再移管** | A/B構成比較が定常運用できる / 監査ログが長期保存できる / コスト・レイテンシSLOが守れる | W9-W12 |

> AWS 不使用方針（2026-05-07）により、Phase 2 の Runtime は Step Functions ではなく Python `asyncio` + LangGraph で確保する。長時間 Durable Execution が必要になる本格運用時は Phase 3 で AWS への再移管を検討する。

---

## 12. 着手時のチェックリスト

実装に入る前に、以下が揃っていることを確認する。

- [ ] IBC側のv2.0方針合意（書面）
- [ ] 予算承認（AWS 不使用方針により当初の970万円から減額見込み）
- [ ] フロントエンドエンジニアのアサイン確定
- [x] **Anthropic API キー発行**（Phase 1 で確認済）
- [ ] **OpenAI API キー発行**（構成3 の GPT-4o 用、2026-05-07 調達可確認済）
- [x] **共通出力スキーマ v0.1 のPydantic定義ファイル**（[apps/agents/src/log_analyzer/schema.py](../../apps/agents/src/log_analyzer/schema.py)）
- [ ] **SQLite スキーマ定義**（results / configs テーブル、W5 着手）
- [ ] 10シナリオの仮設定 + 明星氏フォーマットレビュー
- [ ] 実ログサニタイズ方針（個人情報・機密情報の除去ルール）
- [x] Langfuse / Dify ローカル起動済（Phase 1 完了）
- [ ] GitHubリポジトリ作成 + CI雛形

---

## 付録A: 採用候補ツール一覧（提案v3 Appendix より）

### Agent Framework（5候補）
1. **LangGraph**（採用） — 13/15点、Multi-Agent・状態管理・並列実行で総合首位
2. CrewAI — 12/15点、ロールベース、PoC立ち上げ高速
3. OpenAI Agents SDK — 11/15点、軽量、OpenAIモデル前提
4. AutoGen — 11/15点、対話型、v0.4で過渡期
5. PydanticAI — 9/15点、型安全、Multi-Agent弱

### GUI / Visual Builder（5候補）
6. **Dify**（採用） — 15/15点、Prompt IDE × Multi-model × RAG × API公開
7. Flowise — 12/15点、Apache 2.0
8. Langflow — 12/15点、DataStax傘下
9. Paperclip — 10/15点、組織図コンセプト、新興
10. React Flow — 5/15点（GUI Builderとしては不適、Visualization層で採用）

### AgentOps / Eval（5候補）
11. **LangSmith**（採用） — 14/15点、LangGraph密結合
12. Langfuse — 14/15点、MIT、データレジデンシ要件時の代替
13. Braintrust — 12/15点、商用、CI/CD統合
14. W&B Weave — 12/15点、ML系寄り
15. Helicone — 13/15点、AI Gateway

### Visualization（1採用）
- **React Flow** — 12/12点、MIT、商用実績豊富（Stripe・n8n等）

### Runtime（1採用）
- **AWS Step Functions** — 15/15点、長時間実行（最大1年）・Map並列・CloudTrail監査

---

## 付録B: 用語集

| 用語 | 定義 |
|---|---|
| AgentOps / Eval | AIエージェントの本番運用観点（Trace・精度評価・コスト分析・Prompt版管理）を一元化する基盤領域 |
| Trace | エージェントが行った推論・ツール呼出・分岐の全段階の時系列実行ログ |
| Eval | 事前定義したデータセットや基準で出力品質を定量評価する仕組み |
| Multi-Agent | 複数のAIエージェントが役割分担・連携・引継ぎを行う構成 |
| Orchestration | 複数のステップ・分岐・並列処理を順序立てて制御する仕組み |
| Durable Execution | 実行状態を永続化し、障害発生時に途中再開できる長時間処理基盤 |
| RAG | Retrieval-Augmented Generation。外部知識を検索して回答に利用する手法 |
| BYOC | Bring Your Own Cloud。顧客クラウド上でSaaSを稼働させる提供形態 |
| ラリー型 Multi-Agent | 複数エージェントが結果を相互に渡し合い、合意・反復改善するパターン。本PoCの構成4 |
| Tool Calling | LLMが外部API・関数・DBを呼び出す機能 |
| Human-in-the-loop | 人間の承認・介入を実行フローに組み込む仕組み |
| ASL | Amazon States Language。Step FunctionsのState Machine定義言語 |
| Prompt Versioning | Promptの変更履歴をバージョン管理しA/Bテスト・差分検証を可能にする仕組み |
| State Machine | 状態と遷移を明示的に定義する実行モデル |

---

**この計画書を起点に、W1のチェックリストから着手する。**
