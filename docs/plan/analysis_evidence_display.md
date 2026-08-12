# 解析方針・監視根拠の表示（確認事項 A への対応）実装方針

対象: `docs/reports/observability-qa-2026-08-07.md`（顧客からの確認事項）の **確認事項 A：表示範囲**。
A-1 / A-2 / A-3 の 3 項目に対応した。

## 0. 改修方針サマリ（確定）

> 解析後に「AI が何を前提に、何を根拠に、どれだけのコストで判断したか」を、
> アプリ内（履歴・結果ペイン・レポート）だけで追えるようにする。

| 項目 | 内容 | 対応 |
|---|---|---|
| A-1 | 想定原因（`primary_hypotheses`）・不足データ（`missing_data_notes`）が UI に出ない | **表示追加のみ**（データは保存済み） |
| A-2 | 方針プランナーの消費量（model / tokens / latency）が UI に出ない | **表示追加のみ**（`policy_proposal` 内に保存済み） |
| A-3 | 各監視の findings / evidence / tool_calls が結果に残らない | **保存の新設 ＋ 表示追加** |

前提として A-1 / A-2 は保存済みデータの可視化だけで済み、A-3 のみバックエンドの
保存拡張が必要だった（実行中メモリと Langfuse の Output にしか存在しなかったため）。

## 1. A-1 / A-2: 承認済み解析方針とプランナー消費量

### 表示場所

差し込み口は 2 つで「ライブ / 履歴 × 標準 / チャット」の 4 画面を賄う。

| 画面 | コンポーネント | 表示 |
|---|---|---|
| 標準表示（config-log 解析タブ・解析履歴詳細で共用） | `CombinedResultView` | `PolicySummaryView`（折りたたみ） |
| チャット表示（同上） | `ChatHistoryView` | 方針プランナー発言に全項目 |
| 解析中のライブチャット | `LiveChatView` | `policy_proposal` イベントに全項目 |
| 解析履歴詳細のメタ欄 | `AnalysisHistoryView` | 本解析 / プランナー / 合計 の 3 行 |
| ラウンド別リソース消費 | `RoundMetricsView` | プランナー行（round = `—`） |

表示項目は解析前の確認モーダル（`PolicyProposalModal`）と同じ全項目
（現象要約 / 想定原因 / 調査方針 / 起点監視 / 使用データ / 不足データ・前提 / 着目観点）。
方針ゲートを使わなかった解析は `policy_proposal` 自体が無く、自動的に非表示になる。

### プランナー消費量の扱い（決定事項）

**`metrics.tokens_in/out` には合算しない。** 別枠として表示し、合計は UI 側で計算する。

- 理由: `metrics` は解析履歴一覧・`run_history`・精度比較で使われている既存指標であり、
  定義を変えると**改修前に保存された履歴と比較できなくなる**（同じ列に「本解析のみ」と
  「プランナー込み」が混在する）。
- そのため、プランナー記録がある解析ではラベルに「（本解析）」を付けて意味を明示し、
  「方針プランナー（別枠）」「合計（プランナー込み）」を併記する。
- 将来 `metrics.cost_usd`（確認事項 D-2）を実装する際に「総計」の定義をまとめて決める。

## 2. A-3: 監視ノードの調査根拠

### 保存スキーマ（`schema.py`）

```
MonitorFinding: category / summary / evidence[]
MonitorReport:  round / role / model / confidence / findings[] / tool_calls[]
                rationale / focus_hint_received / focus_hint_for_next
                truncation_note / parse_error
```

- `AnalysisResult.monitor_reports`: 主 Stage 分（`delegation_history` と同じ扱い）
- `StageOutput.monitor_reports`: Stage ごと（2 段階解析用）
- いずれも `default_factory=list` の追加専用フィールド。**`schema_version` は v0.2 のまま**。

### 収集経路

`rally_agent.run_rally_stream` の委譲ループで、監視 1 回ごとに `_build_monitor_report()` を
通して蓄積し、`_build_analysis_result()` で結果に載せる。2 段階は `rally_two_stage` が
Stage 出力と統合結果の双方に伝播する。

既存の `state["monitor_results"]` とは用途が異なる（あちらは**次の監視へ渡す参考材料**、
こちらは**保存・レビュー用**）ので併存させている。

LLM 出力はブレるため `_build_monitor_report()` で正規化する（evidence が文字列 1 件、
findings が文字列配列、キー欠落など）。

### 保存上限（決定事項）

evidence は**モデルが引用したログ本文そのもの**であり、履歴 DB（`result_json`）の肥大と
機微データ保持量の増加に直結するため上限を設ける。定数は `rally_agent.py` に集約。

| 対象 | 上限 | 超過時 |
|---|---|---|
| evidence 1 件の文字数 | 500 字 | 末尾を `…` で切る |
| findings（1 監視あたり） | 10 件 | 以降を捨てる |
| evidence（1 finding あたり） | 5 件 | 以降を捨てる |
| tool_calls | 20 件 | 以降を捨てる |

切り詰めた内容は `truncation_note` に記録し、UI に「※ 保存時に一部省略: …」と表示する。

**保存しないもの**: BigQuery から取得した行そのもの（`bq_evidence`）。これは確認事項 B-2 の
論点（実 SQL・取得行の記録）であり、機微データの保持範囲を別途決める必要があるため
A-3 では踏み込まない。evidence としてモデルが引用した範囲のみが残る。

### 表示場所

| 画面 | 表示 |
|---|---|
| 標準表示（結果ペイン） | 「監視ノードの調査根拠（N ノード）」セクション（`MonitorEvidenceSection`） |
| チャット表示 | 各監視の発言内に折りたたみ |
| 委譲チェーン履歴 | 各ラウンドに折りたたみ（トポロジー解析タブ用。`showMonitorEvidence` で切替） |
| Markdown レポート | 「調べたこと（所見と根拠）」「実行したツール」 |

**重複防止**: 同じ根拠を 1 画面に二度出さないよう、結果ペインにセクションを出す画面では
`DelegationHistoryView` 側の埋め込みを `showMonitorEvidence={false}` で抑止する。

config-log 解析の標準表示には委譲チェーン履歴が無いため、結果ペインの独立セクションが
標準表示での唯一の表示場所になる（当初は委譲チェーン履歴のみに埋め込んでいたが、
標準表示で見えないため追加した）。

### 後方互換 / マイグレーション

**DDL 変更もマイグレーションも不要。**

- `monitor_reports` は `analysis_history.result_json`（TEXT に AnalysisResult 全体を JSON
  保存）の中に入るため、テーブル定義は変わらない。`storage.py` は未変更。
- 本リポジトリはマイグレーションファイル方式ではなく、`storage.py::init_db()` が起動時に
  `CREATE TABLE IF NOT EXISTS` ＋条件付き `ALTER TABLE` を実行する方式（SQLite /
  PostgreSQL 両対応）。列追加が要る改修ならここに追記する。
- 改修前に保存された履歴は `monitor_reports` を持たないが、Pydantic 側は
  `default_factory=list`、TS 側は任意フィールドのため**そのまま読める**。UI では
  「監視ごとの調査根拠は保存されていません（対応前に実行された解析です）」と明示する。

## 3. 検証

- バックエンド: `apps/agents/tests/test_monitor_reports.py`
  （正規化 / 切り詰めと注記 / parse_error 保持 / AnalysisResult 格納 / 旧 JSON の後方互換 /
  LLM をモックしたストリーム実行で 2 監視分が final に載ること）
- UI: `tsc -b` と `vite build`
- 実機: 方針ゲート ON の config-log 解析を実行し、標準・チャット・レポート・解析履歴の
  4 面で表示を確認（`monitor_reports` に findings 4 / evidence 15 / tool_calls 13 が保存され、
  「evidence 1 件を省略」の注記まで出ることを確認）

## 4. 積み残し（確認事項 A の範囲外）

- B-2: 発行 SQL と取得行の記録（保持範囲の検討が必要）
- B-1: 方針プランナー・監査GPT の Langfuse 計装
- D-2: `metrics.cost_usd` の実計算（プランナー・監査GPT 分を含む「総計」の定義）
