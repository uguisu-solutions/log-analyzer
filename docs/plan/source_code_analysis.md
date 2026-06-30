# 設計プラン：ソースコードを解析対象に追加（オンデマンド参照ツール ＋ DBスキーマ抽出）

> 対象：config-log 解析タブ。
> 関連：[config_log_stages.md](./config_log_stages.md) / [mermaid_and_policy_gate.md](./mermaid_and_policy_gate.md) /
> 既存のオンデマンド取得の先行例＝BigQuery ツール（[rally/tools.py](../../apps/agents/src/log_analyzer/rally/tools.py)）。

> **状態（2026-07-01）**: **Phase 1・Phase 2 実装完了**（branch `feature/source-code-analysis`）。
> - Phase 1: 取り込み（複数アップロード・zip展開・除外・zip-slip・50MB）／決定論インデックス
>   （Python=ast, TS/JS=tree-sitter）／DBスキーマ抽出（DDL=sqlparse, ORM=SQLAlchemy/Django/Prisma）／
>   `/api/source` CRUD ＋ tree。新規 `apps/agents/src/log_analyzer/source/`（indexer/db_schema/codebase）。
> - Phase 2: オンデマンド参照ツールを rally 監視ループに組み込み。新規 `rally/source_tools.py`
>   （`source_search`/`source_read`/`db_schema` ＋ 予算/重複ガード ＋ log_text 注入ブロック ＋ SourceContext）。
>   `monitors.py` / `rally_agent.py` / `rally_two_stage.py` / `api.py`（`source_codebase` 受け）を結線。
>   input トークン配慮（§3）: search は署名のみ／read は1回6000字＋関数スライス／run 全体40000字の
>   ソフト上限／同一 path・symbol の重複ガード／DBスキーマは要約注入＋詳細はツール、を実装。
> - テスト: source 系 5 本（67 ケース）、全 223 件通過。
> - Phase 3（UI）: `SourceCodebasePanel.tsx`（コードベース一覧/選択/複数アップロード）、
>   `SourceReferenceView.tsx`（ノード別「参照したソース」＋DBスキーマ）を追加。
>   `ConfigLogAnalysis.tsx`（ソース選択パネル配置・`source_codebase` 送出・結果表示）、
>   `types.ts`（SourceContext/DbSchema 等）、`reasoningReport.ts`（参照ソース節）、`App.css` を更新。
>   tsc / vite build 通過。
> **Phase 4（将来: 起点ダイジェスト / 言語追加 / 呼び出しグラフ / ノード単位マッピング）は未着手。**

## 確定した前提（ユーザー確認済み 2026-07-01）

| 論点 | 決定 |
|---|---|
| ソースと障害の結びつけ | **独立コードベースとして投入**（ノード非依存で取り込む）。 |
| **参照フロー** | **オンデマンドツール方式（BigQuery と同型）**。各監視ノードが tool-use ループの中で、自分の観点で必要なソースだけを `source_search` / `source_read` で取得する。事前一括の静的注入はしない。 |
| 対象言語 | **Python ＋ JavaScript/TypeScript**。 |
| DBスキーマ源 | **SQL DDL ファイル（.sql / migrations）** ＋ **ORM モデル（SQLAlchemy / Django / Prisma 等）**。 |
| 取り込み | **複数ファイルアップロード**（zip / 単体ソース混在可）。**合計 50MB 上限**。サーバが `samples/source/<name>/` に展開・集約。 |
| 選別モデル | 本解析モデル `claude-opus-4-7`。ソース選別は各ノード自身が tool-use で行うため、別建ての安価選別モデルは置かない（将来オプション、§7）。 |

> **方式変更の経緯**：当初は「安価モデルで事前に1回選別→ダイジェストを全ノードに静的注入」（2段階フィルタ）で設計していたが、
> 「各ノードが必要に応じて参照する」要件に合わせ、**BigQuery と同型のオンデマンドツール方式**へ全面変更した。
> トークン最小化は「事前選別」ではなく「決定論インデックス＋ツールが返す量の上限＋各ノードが必要分だけ取得」で担保する。

## 設計の基本方針

1. ソースコードは**ツール経由でオンデマンド参照する第3のデータ源**（既存の BigQuery ログ取得と同じ思想）。推論エンジン（rally / config-log 2段階）の骨格は変えず、**ツールを足すだけ**。
2. 全量はLLMに渡さない。決定論インデックス（コスト0）を裏に持ち、**ツールは「署名・検索結果・関数スライス」を上限つきで返す**。各ノードが自分の観点で必要分だけ引く。
3. **DBスキーマはコンパクトかつ高価値なので `log_text` に常時注入**（テーブル一覧＋列）。詳細は `db_schema` ツールでも引ける。
4. ソース未指定なら従来挙動と完全一致（後方互換）。

---

## 1. ディレクトリ配置と取り込み

ログ（`samples/logs/`）・トポロジー（`samples/topology/`）と同列で専用ルートを新設する。
**コードベースは UI から複数ファイルをまとめてアップロードし、サーバが `samples/source/<codebase_name>/` に集約する。**

```
samples/source/                # アプリ内の専用ルート
  <codebase_name>/            # アップロード1回＝コードベース1件＝1ディレクトリ
    app/...                   # zip はディレクトリ構造を保持して展開、単体ソースはそのまま配置
    db/schema.sql
    models/...
```

- **複数アップロード対応**：1回のアップロードで複数ファイル可（zip 複数 / 単体ソース複数 / 混在）。zip は展開、単体はそのまま配置し、同一コードベースに集約。
- **合計サイズ上限：50MB**（全ファイルの合計を逐次加算し、超過時点で打ち切り）。zip は展開後サイズも加算（zip 爆弾対策）。
- **除外ルール（展開時とインデックス時の二段でハード除外）**：`node_modules` `.venv` `venv` `.git` `dist` `build` `__pycache__` `.ruff_cache` `.pytest_cache` `coverage`、ロックファイル、`*.min.js`、バイナリ・画像、`_MAX_FILE_BYTES`（既定 512KB）超のファイル。
- **zip 展開のセキュリティ**：zip-slip（`../`）を弾き、展開先が `samples/source/<name>/` 配下に収まることを検証。シンボリックリンクエントリは無視。
- パス/コードベース名のバリデータは既存 `_safe_log_path`（[api.py:293](../../apps/agents/src/log_analyzer/api.py#L293)）と同型で用意。
- トークン量は取り込みサイズに依存しない。LLM へ渡る量は「各ノードが何回・何をツールで引いたか」とツールの結果上限だけで決まる（§3）。

---

## 2. 処理パイプライン

```
[A] 取り込み時：決定論インデックス構築 (コスト0, LLM不使用)
      ├ 展開 + 除外 → ファイル走査
      ├ シンボル抽出 (関数/クラス/export) : Python=ast, TS/JS=tree-sitter
      └ DBスキーマ抽出 (DDL sqlparse + ORM)
            ↓ SourceIndex / DbSchema を JSON でキャッシュ (samples/source/<name>/.index.json)

[B] 解析開始時：log_text に「ソース利用可能」マーカー + DBスキーマ要約を注入
      （BigQuery の [ログ取得元: BigQuery] マーカーと同じ要領）
            ↓

[C] 本解析中：各監視ノードが tool-use ループでオンデマンド取得  ← ここが本要件
      orchestrator / fw / routing / app / dns / sec / integrator が
        ├ source_search(query)   関連ファイル・関数を検索 (署名のみ, 上限つき)
        ├ source_read(path,sym)  特定の本文を取得 (関数スライス, 文字数上限)
        └ db_schema(table?)      DBスキーマを取得 (任意, log_text 注入の補完)
      を「自分の観点で必要なときだけ」呼ぶ。各取得結果はそのノードのコンテキストに積まれる。
```

工程A＝裏側のデータ源（取り込み時に1回）。工程C＝各ノードの能動参照（要件の中心）。

### 工程A：決定論インデックス（新規 `source/indexer.py`）

- `build_source_index(root: Path) -> SourceIndex`
  - 各ファイルの `path / language / bytes / lines / symbols[]` を収集。
  - **Python**：標準ライブラリ `ast`（最も正確・依存ゼロ）。
  - **TS/JS**：`tree-sitter` ＋ `tree-sitter-language-pack` で AST 抽出。パース例外は1ファイル単位で握りつぶし、そのファイルはシンボル空で残す（全体は止めない）。
- インデックスは取り込み時に1回構築し `samples/source/<name>/.index.json` にキャッシュ（解析のたびに再走査しない）。
- `search(query, *, lang=None, limit=20)`：識別子・パス・障害シグナルで関連ファイル/シンボルをランキングして返す（`source_search` ツールの裏）。
- `read(path, *, symbol=None, max_chars)`：ファイル/関数スライスを返す（`source_read` ツールの裏）。

### 工程B：注入（最小限のマーカー＋DBスキーマ）

- `_build_topology_log_text`（[api.py:538](../../apps/agents/src/log_analyzer/api.py#L538)）に source 用ブロックを追加：
  ```
  ## ソースコード（オンデマンド取得）
  このインシデントには解析対象のソースコードがあります（コードベース: <name>）。
  全文は与えません。source_search で関連ファイル・関数を探し、source_read で必要な
  箇所だけ取得してください。闇雲に全件読まず、ログのエラー/例外/識別子に絞ること。

  ## DB スキーマ（要約）
  - payments(id PK, user_id FK→users.id, status, ...)  [ddl+orm]
  - users(id PK, email, ...)
  （テーブル数が多い場合は上位 N＋件数注記。各テーブルの詳細列定義は db_schema(table) で取得）
  ```
- DBスキーマは**要約のみ注入**（テーブル名＋主要列）。詳細列定義やコード本文は注入せずツールで引く（§3 input トークン配慮）。

### 工程C：オンデマンド参照ツール（新規 `source/source_tools.py`）

BigQuery ツール（[rally/tools.py:113-353](../../apps/agents/src/log_analyzer/rally/tools.py#L113)）を範として実装。

- **ツールスキーマ**（Anthropic Messages API の `tools=` に渡す）
  - `source_search` … `{ query: str, lang?: "py"|"ts"|"js", limit?: int }` → 関連ファイルパス＋シンボル署名＋一致理由（**本文は返さない**、`bigquery_schema` に相当する軽量呼び出し）。
  - `source_read` … `{ path: str, symbol?: str, start_line?: int, end_line?: int }` → 本文（symbol 指定時は関数単位スライス＋前後文脈）。
  - `db_schema` … `{ table?: str }` → DBスキーマ（注入済み要約の詳細版）。
- **実行関数**：`run_source_search(tool_input, codebase) -> str` / `run_source_read(...)` / `run_db_schema(...)`。
  - **許可リスト**：参照可能なのは run で指定された `codebase` 配下のみ（BQ の `allowed_sources` 検証と同型）。パストラバーサルは絶対パス解決で再検証。
  - **結果の予算化**（BQ の `_max_rows_in_context` / `_max_result_chars` と同型、環境変数で調整）：
    - `source_read` 1回の文字数上限（既定 ~6000 字）、超過は中央省略。
    - `source_search` の最大ヒット件数（既定 30）。
    - 1 run あたりのソース取得総文字数のソフト上限（既定 ~40000 字）。超過後はツールが「予算超過。さらに絞れ」を返す。
  - 失敗時も例外を投げず文字列で返す（LLM が graceful に継続）。

### 工程D：rally への組み込み

- 監視ノードの tool-use ループ（BQ ツールを処理している箇所＝`rally/monitors.py` 周辺）に source 系ツールを追加登録。
  - `tools=` に BQ ツール＋ source ツールを併せて渡す。
  - tool_use ディスパッチに `source_search` / `source_read` / `db_schema` の分岐を追加。
- ツールが使えるのは「コードベースが指定された run」のみ。未指定なら従来どおりツールを渡さない（後方互換）。
- 2段階モードでは stage1 / stage2 双方でツールを有効化。

---

## 3. input トークンが膨大にならない配慮（最重要）

オンデマンドツール方式は、**tool 結果が会話コンテキストに累積し、以降のツール往復で毎ターン再送される**のが input トークンの最大リスク（BigQuery ツールが結果を厳しく切り詰めているのと同じ理由）。出力（1回の結果サイズ）だけでなく、**累積 input** を設計で抑える。

### 1回あたりを抑える
| 層 | 効きどころ |
|---|---|
| 取り込み | 除外ルールで `node_modules` 等を**そもそも書かない**／合計50MB上限 |
| 工程A | インデックスは署名のみ。本文はキャッシュせず必要時に読む |
| `source_search` | **本文を返さない**（署名・パス・理由のみ）。ヒット件数上限（既定30） |
| `source_read` | 1回の文字数上限（既定 ~6000 字）＋関数単位スライス。超過は中央省略 |
| DBスキーマ注入 | 注入は**テーブル名＋列名の要約**まで。大規模スキーマ（テーブル多数）は上位 N＋件数注記に丸め、詳細は `db_schema(table)` で都度取得（注入が膨らまない） |

### 累積（再送）を抑える ← input トークンの本丸
| 施策 | 内容 |
|---|---|
| **run 全体のソース取得総量にソフト上限** | 既定 ~40000 字。超過後はツールが取得を拒否し「これ以上は読めない。絞れ」を返す（暴走停止） |
| **1ノードあたりのツール呼び出し回数上限** | 既定 ~6 回（環境変数で調整）。BQ と同様、無限往復で context が膨れるのを防ぐ |
| **過去 tool 結果の要約折りたたみ** | 同一ノードが新しい `source_read` をするとき、**古い tool 結果は「path:lines だけの1行サマリ」に圧縮**してコンテキストに残す（全文を毎ターン再送しない）。直近 1〜2 件のみ全文保持 |
| **重複取得のガード** | 同じ path/symbol を再度 `source_read` したら、本文を再送せず「取得済み（前述）」を返す |
| **ノード間でコンテキストを引き継がない** | 各監視ノードは自分が引いたソースだけを持つ。rally の委譲時に他ノードの tool 結果全文は渡さない（要約のみ） |

### 計測で可視化
- `SourceContext.total_chars_fetched` と `SourceToolCall.result_chars` を記録し、`info_loss_flags` に「source fetched: N chars across M calls（うち K 件は要約折りたたみ）」を残す。
- 上限ヒット時は SSE / レポートに「ソース取得が上限に達したため一部未取得」と明示（filters.py の `info_loss` と同思想）。

→ これにより、50MB のコードでも LLM へ流れる input は「各ノード ≤6回 × ≤6000字 − 折りたたみ分」かつ「run 全体 ≤40000字」でキャップされ、累積再送でも膨張しない。各上限は環境変数で調整可能。

---

## 4. スキーマ（[schema.py](../../apps/agents/src/log_analyzer/schema.py)）追加

- `SourceSymbol`：`name / kind(function|class|method|export) / start_line / end_line`
- `SourceFile`：`path / language / bytes / lines / symbols[]`
- `DbColumn` / `DbTable` / `DbSchema`：テーブル・列・PK/FK/index・出典（ddl/orm）
- `SourceToolCall`：`round / node / tool(source_search|source_read|db_schema) / args / result_chars`（**どのノードが何を引いたか**の記録＝再現とUI表示用）
- `SourceContext`：`codebase / db_schema / tool_calls[] / total_chars_fetched / index_stats`
- `AnalysisResult` への影響（後方互換・既定空）：`source_context: SourceContext | None = None` を追加。ツール消費トークンは `metrics.tokens_in/out` に合算（BQ と同様、tool-use ぶんは本解析モデルのトークンに乗る）。

---

## 5. API 変更（[api.py](../../apps/agents/src/log_analyzer/api.py)）

### 新規エンドポイント（samples/logs の CRUD と同型）
| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/source` | コードベース一覧（名前 / ファイル数 / 合計bytes / 言語内訳 / DBテーブル数） |
| POST | `/api/source` | **複数ファイルアップロード**（`files: list[UploadFile]`）→ 検証・展開・集約・インデックス構築 |
| GET | `/api/source/{name}/tree` | ファイルツリー＋シンボル署名＋DBスキーマ（インデックスのプレビュー、本文なし） |
| DELETE | `/api/source/{name}` | コードベース削除（ディレクトリ＋キャッシュごと） |

- アップロードは既存 `upload_log`（[api.py:1058](../../apps/agents/src/log_analyzer/api.py#L1058)）と同型：各ファイルをチャンク書き込み→合計加算チェック→zip展開→失敗時はディレクトリごとクリーンアップ。上限合計50MB／5000ファイル（二次ガード）。同名既存は 409。

### リクエスト型拡張
- `ConfigLogRunRequest`（[api.py:1615 付近](../../apps/agents/src/log_analyzer/api.py#L1615)）に `source_codebase: str | None = None`（未指定で従来挙動）。
- ハンドラ：指定時にインデックスをロード（無ければ構築）→ `_build_topology_log_text` に DBスキーマ＋マーカーを注入 → rally 実行時に source ツール許可リスト（codebase）を渡す。`source_context` を `AnalysisResult` と解析履歴 `request_json` に保存。

### SSE プロトコル追加（BQ の tool イベントと同様、ライブチャットに出す）
| イベント | data | emit 箇所 |
|---|---|---|
| `source_tool_call` | `{ round, node, tool, args }` | 監視ループ（ツール呼び出し時） |
| `source_tool_result` | `{ round, node, tool, summary, chars }` | 監視ループ（結果確定時） |

→ 「どのノードが・どのラウンドで・どのファイルを読んだか」がライブチャット（[LiveChatView](../../apps/ui/src/LiveChatView.tsx)）に流れる。

---

## 6. UI 変更（[ConfigLogAnalysis.tsx](../../apps/ui/src/ConfigLogAnalysis.tsx) / [types.ts](../../apps/ui/src/types.ts)）

- 入力エリアに「ソースコード」セクション：コードベース選択（`/api/source`）＋**複数ファイルアップロード**（`<input type="file" multiple>`、合計50MB目安を表示）＋「ソース解析を有効化」トグル。
- 送信ボディに `source_codebase` を追加。
- SSE `source_tool_*` を会話・進捗に表示（BQ 取得の表示に倣う）。
- 解析後カードに **ノード別「参照したソース」一覧**（どのノードがどの関数を読んだか）＋DBスキーマを表示する小コンポーネント（`SourceReferenceView.tsx` 新規）。`reasoningReport.ts` にも反映。
- `types.ts` に `SourceContext` / `SourceToolCall` / `DbSchema` 等の型を追加。解析履歴の再現に乗る。

---

## 7. 決定事項 / 残課題

**確定（2026-07-01）**：
- 参照フロー＝**オンデマンドツール（BigQuery 同型）**。各ノードが `source_search` / `source_read` / `db_schema` を自分の判断で呼ぶ。
- パーサ：Python=`ast`、TS/JS=`tree-sitter`＋`tree-sitter-language-pack`、DDL=`sqlparse`。いずれも Phase 1 から。
- DBスキーマは `log_text` に**要約注入**（テーブル名＋主要列）＋詳細は `db_schema(table)` ツールで取得。
- **input トークン配慮**（§3）：tool 結果の累積再送を、1回上限・run 総量上限・呼び出し回数上限・過去結果の要約折りたたみ・重複/ノード間引き継ぎ抑制で抑える。
- 取り込み＝複数ファイルアップロード／合計50MB／zip展開・除外・zip-slip検証。
- 入力経路＝当面 config-log 解析タブのみ。

**残課題（将来オプション）**：
1. **安価モデルによる起点ダイジェスト**（ハイブリッド）：解析開始前に安価モデルで「まず読むべき数ファイル」を選び小さなダイジェストを注入し、ツール往復を減らす。当面は不要、必要になれば追加。
2. **呼び出し回数の上限/ガード**：1ノードあたりの source ツール最大呼び出し回数（暴走防止）。BQ と同様に環境変数で。
3. **言語追加**（Java/Go）、**関数呼び出しグラフ**による関連箇所の自動拡張。

---

## 8. フェーズ分割

- **Phase 1（インデックス＋取り込み）**：複数アップロード／展開・除外・zip-slip、`ast`＋`tree-sitter` インデックス、`sqlparse` DDL＋ORM 抽出、`.index.json` キャッシュ、`/api/source` CRUD、`tree`/UI でのプレビュー。→ ここまでで「取り込み・解析・スキーマ抽出」が単体検証可能。
- **Phase 2（オンデマンドツール）**：`source_tools.py`（search/read/db_schema）＋ rally 監視ループへの組み込み＋許可リスト＋結果予算化＋`log_text` マーカー/スキーマ注入。→ 要件の中心。
- **Phase 3（可視化・再現）**：SSE `source_tool_*`、ノード別参照ビュー、`source_context` の解析履歴再現、reasoningReport 反映。
- **Phase 4（将来）**：起点ダイジェスト（ハイブリッド）、呼び出し上限、言語追加、呼び出しグラフ拡張、ノード単位マッピング。

---

## 9. 変更/新規ファイル一覧

| 区分 | ファイル | 内容 |
|---|---|---|
| BE 新規 | `apps/agents/src/log_analyzer/source/indexer.py` | 走査・除外・シンボル抽出（ast/tree-sitter）・search/read・索引キャッシュ |
| BE 新規 | `apps/agents/src/log_analyzer/source/db_schema.py` | DDL（sqlparse）＋ORM からのスキーマ抽出/マージ |
| BE 新規 | `apps/agents/src/log_analyzer/source/source_tools.py` | `source_search`/`source_read`/`db_schema` のツールスキーマ＋実行関数（許可リスト・予算化） |
| BE | `apps/agents/src/log_analyzer/rally/monitors.py` | 監視 tool-use ループへ source ツールを登録・ディスパッチ |
| BE | `apps/agents/src/log_analyzer/schema.py` | SourceFile/Symbol/DbSchema/SourceToolCall/SourceContext 追加、AnalysisResult 拡張 |
| BE | `apps/agents/src/log_analyzer/api.py` | `/api/source` CRUD、リクエスト型、注入、ツール許可リスト受け渡し、SSE、履歴保存 |
| FE | `apps/ui/src/ConfigLogAnalysis.tsx` | ソース選択/複数アップロード/トグル、送信ボディ、SSE 処理 |
| FE 新規 | `apps/ui/src/SourceReferenceView.tsx` | ノード別「参照したソース」＋DBスキーマ表示 |
| FE | `apps/ui/src/types.ts` / `reasoningReport.ts` | 型追加・レポート表示 |
| テスト | `apps/agents/tests/test_source_indexer.py` / `test_source_tools.py` ほか | インデックス・DDL/ORM 抽出・ツール許可リスト/予算化・後方互換 |
| 依存 | `apps/agents/pyproject.toml` | `tree-sitter` ＋ `tree-sitter-language-pack`、`sqlparse` を追加 |
| サンプル | `samples/source/<scenario>/` | 検証用の小規模コードベース＋DDL/ORM |
