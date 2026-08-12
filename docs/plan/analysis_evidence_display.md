# 解析の記録・表示の改善（確認事項 A・B）実装方針

対象: `docs/reports/observability-qa-2026-08-07.md`（顧客からの確認事項）のうち、
**確認事項 A：表示範囲**（A-1 / A-2 / A-3）と **確認事項 B：記録範囲**（B-1 / B-3 / B-4）。
B-2 と確認事項 C・D は未着手（末尾「積み残し」参照）。

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

## 4. 確認事項 B への対応（B-1 / B-3 / B-4）

### B-3: Langfuse のレイテンシ（計装漏れの修正）

`trace.generation()` に `start_time` / `end_time` を渡していなかったため Langfuse の
Latency が常に 0.00s だった。各ノードは `perf_counter` の経過時間しか持たないので、
**呼び出し側（`rally_agent`）で絶対時刻（UTC）を測って** token_log に積み、Generation に渡す。
ノード 3 ファイル（orchestrator / monitors / integrator）は無改修。

併せて、JSON パースに失敗したノードは `level="WARNING"` + `status_message` を付け、
「なぜ integrator にフォールバックしたか」をトレース上で追えるようにした。

なお `metrics.latency_ms_total` は実測の壁時計時間で、Generation の合計とは一致しない
（ノード外の待機を含むため）。「所要時間は履歴側が正」という整理は維持する。

### B-1: プランナー・監査GPT の Generation 化

| 対象 | 実行位置 | 実装 |
|---|---|---|
| 監査GPT | 1 段階は rally 内、2 段階は `_attach_audit`（トレース外） | `run_audit(trace_id=...)` を追加し、関数内で `client.generation(trace_id=...)` を送る。2 段階は監査対象である最終 Stage のトレースに紐付く |
| 方針プランナー | rally より**前**（トレース未生成） | `run_started` で trace_id が確定した時点で api.py が後付けする（2 段階では Stage 1 のトレース） |

- 検討した代替案: ①api.py 側で先に trace を作り rally に渡す → 3 エンドポイントの署名変更が波及、
  ②プランナー専用の独立トレース → 解析トレースと分離して追跡性が落ちる。いずれも不採用。
- 監査は失敗時も Generation を残す（動いたかどうかすら追えない状態を避けるため）。
- `gpt-5.5` を価格表に登録（$5 / $30 per MTok、2026-08 時点）。監査のコストも Langfuse に出る。
- **方針却下時**はプランナーだけ動いて rally が始まらず trace_id が発行されないため、
  この経路は Generation ではなく B-4 の失敗記録として残す。

Langfuse は実消費の記録、アプリの `metrics` は既存履歴との比較互換を優先、という
役割分担は維持する（A-2 の「metrics に合算しない」方針は変えない）。

### B-4: 失敗・中断の記録

**DB スキーマ変更あり**（A-3 と違いここは列追加が必要）。

- `run_history` に `status`（`ok` / `error` / `aborted` / `rejected`）、`error_stage`、
  `error_message` を追加。`init_db()` に条件付き `ALTER TABLE` を書く既存方式に合わせたので、
  **本番反映はデプロイして再起動するだけ**。既存行は `DEFAULT 'ok'` で埋まる（従来は正常終了しか
  記録していなかったため実態と一致）。
- 記録経路: プランナー例外 / 方針却下 / rally 例外 / 結果なし / クライアント切断（`finally` で
  final 未到達を検知）/ バリデーションエラー（422・400）。対象は config-log / topology /
  `runs/stream` の 3 エンドポイント。
- バリデーションエラーは FastAPI の例外ハンドラで拾う。解析エンドポイント以外は記録しない
  （ノイズ回避）。これが顧客指摘の「トークン 0 で履歴に無い実行」の正体。
- 結果が無いので完全再現用の `analysis_history` ではなく、軽量メタの `run_history` に残す。
- **表示**: 「実行履歴」タブは現在 UI 上で非表示のため、顧客が実際に見る**解析履歴タブの上部**に
  「失敗・中断した実行」セクション（既定は折りたたみ、0 件なら非表示）を出す。`status=failed`
  （= ok 以外）で取得する。実行履歴タブ側にも結果列・フィルタ・詳細を追加済み（再表示した場合用）。
- テストが開発用 DB を汚さないよう `tests/conftest.py` で SQLite を一時ディレクトリに隔離した
  （400/422 を検証する既存テストが実 DB に書き込むようになったため）。

## 5. 積み残し

- B-2: 発行 SQL と取得行の記録（機微データの保持範囲の検討が必要）
- C-1: 仕様の明確化のみで改修不要
- D-1: 委譲上限 0 の表示バグ / D-2: `metrics.cost_usd` の実計算 / D-3: `schema_version` v0.1 固定
