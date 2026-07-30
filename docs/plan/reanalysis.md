# 再解析（前回推論を種にした再調査）実装方針

## 0. 改修方針サマリ（確定）

### 改修方針（一文）

> 過去の解析履歴画面から、前回解析の推論結果を引き継ぎ、追加のコメント（問診票）・
> 新規ログ・BigQuery テーブルを加えて、再度解析を回せるようにして、
> 再解析の結果を元の解析と紐づけて履歴で確認できるようにする。

- **位置づけ**: 既存機能を活かした最小限の改修。
  - 前回推論の引き継ぎ → `result_json` を `buildReasoningReport` で要約（既存関数）
  - 追加コメント / 新規ログ / BQ → 既存の入力口をそのまま流用
  - 実行 → 既存エンドポイント `/api/runs/config-log-stream` を再利用（新規解析ロジック不要）
- **Langfuse Trace Detail は使わない**: 前回推論は `result_json` に揃っており、整形関数も
  既存。生トレースはノイズが多く手動コピーも要るため、アプリ内要約の方が明確に優位。

### 段階分け

| 段階 | 内容 | 規模 |
|---|---|---|
| 第一段階（コア） | 再解析を回す（前回推論の引き継ぎ＋追加コメント/新規ログ/BQ） | 最小 |
| 第二段階 | 再解析結果を元解析と紐づけて履歴で版管理・トグル表示 | 小さな追加（系譜3列） |

第一段階だけで「再解析が回る」体験は完結する。版管理が必要なら第二段階を乗せる。

### 未決点（実装着手前に判断）

各項目に既定（未指定なら採る方針）を併記する。**着手を止めるのは #1 のみ**。
#2〜#5 は既定のまま進められ、必要時に差し替える。

| # | 未決点 | 選択肢 | 既定 |
|---|---|---|---|
| 1 | **スコープ** | 第一段階のみ先行 / 第一＋第二を一括 | **唯一のブロッカー。要判断** |
| 2 | 2 段階モード（`_gen_two_stage`）対応 | 1 段階のみ / 2 段階も対応 | まず 1 段階のみ |
| 3 | 一覧の件数上限（limit=200） | MVP（クライアント側グループ化）/ 堅牢版（最新のみ取得＋展開時に遅延取得） | まず MVP |
| 4 | 中間版削除時の系譜の繋ぎ方 | 子の親を祖父へ付け替え / 削除禁止 / 気にしない | 後回し可 |
| 5 | 追加コメントの入れ口 | 既存問診票に相乗り / 専用「追加所見」欄を新設 | まず問診票に相乗り |

### 先方向け説明（3 行）

> ご認識のとおり現状は過去履歴に情報を加えて再調査する仕組みはありませんが、実装は可能です。
> 前回解析の推論結果を引き継ぎ、追加のコメント・新規ログ・BigQuery テーブルを加えて再度解析を
> 回せる形を想定しています。再解析の結果は元の解析と紐づけて履歴で確認できるようにします。

---

## 1. 目的 / スコープ

過去の解析結果に情報を加えて「再調査」を回せるようにする。ただし本方針では
**解析対象の生ログ・設定ファイル本文は保存しない**。控えるのは対象ファイル名程度に留め、
**前回解析の "推論内容"（`result_json`）を種にして再解析**する。

- 対象: config-log 解析（`config4` / rally ベース、`POST /api/runs/config-log-stream`）
- 前回の推論はすでに `analysis_history.result_json`（`AnalysisResult` 全体）に保存済み。
  テキスト化関数 `buildReasoningReport(result)`（`ui/src/reasoningReport.ts:142`）も既存。
- 追加情報として **自由記述 / 新規ログファイル / 新規 BQ テーブル** を指定できる。
- 再解析結果は前回結果と系譜（親子・世代）で紐づけ、履歴一覧で版管理する。

### この方式の性質（合意事項）

生ログを再投入しないため、再評価の材料は「**前回推論が言語化できた範囲＋今回の追加情報**」が
上限。元ログにしか無い事実は復元されない。追加情報（新証拠）で前回の見立てを更新する用途に向く。
※ 追加情報として新規ログ/BQ を足した場合は、その分は生データとして解析に入る。

---

## 2. 全体像（データフロー）

```
[履歴詳細画面]
  └ 保存済み result_json（＝前回の推論すべて）
        │ ① buildReasoningReport() で Markdown 要約に変換（既存関数を流用）
        ▼
  「前回推論サマリ」テキスト
        │
        ├─ ② 追加情報（任意・複数併用可）
        │     - 自由記述        → questionnaire_answers（既存注入経路）
        │     - 新規ログファイル → node_logs（新規アップロード分だけ）
        │     - 新規 BQ テーブル → node_bigquery（実行時ライブ取得）
        ▼
  ③ 既存 /api/runs/config-log-stream に POST（前回の生ログは送らない）
        │ ④ 前回サマリを log_text 先頭に注入（policy_prefix/source_block と同じ方式）
        ▼
  ⑤ 既存 rally をそのまま実行 → 新しい AnalysisResult
        │ ⑥ 保存時に系譜（root_run_id / parent_run_id / revision）を付与
        ▼
  [履歴一覧] 調査単位でグループ表示・古い版はトグルで開閉
```

**新エンドポイント・新解析ロジック・実行本体の改修は不要**。既存パイプラインを再利用する。

---

## 3. データモデル変更（B 案: 系譜 3 列）

`analysis_history` テーブル（`storage.py:239-262`）に 3 列を追加する。

```sql
ALTER TABLE analysis_history ADD COLUMN parent_run_id TEXT;              -- 直近の親（差分表示用）。NULL = 大元
ALTER TABLE analysis_history ADD COLUMN root_run_id   TEXT;              -- 大元（束ねる用）。大元自身は自分の run_id
ALTER TABLE analysis_history ADD COLUMN revision      INTEGER DEFAULT 0; -- 0=初回, 1,2,… = 再解析世代
```

- SQLite / PostgreSQL 両対応（`DATABASE_URL` 切替。`storage.py:40-44`）。
- 既存行は `parent_run_id=NULL` / `root_run_id=NULL`（＝大元扱い）でそのまま動く。
  必要なら移行時に `root_run_id = run_id`, `revision = 0` を既存行へ補完する。

### 系譜の値の付け方

| 種別 | parent_run_id | root_run_id | revision |
|---|---|---|---|
| 初回解析 | NULL | 自分の run_id | 0 |
| 1 回目の再解析 | 大元の run_id | 大元の run_id | 1 |
| 2 回目の再解析 | 前回の run_id | 大元の run_id | 2 |
| 分岐（同じ親から別再解析） | 親の run_id | 大元の run_id | 親.revision + 1 |

系譜イメージ:

```
root A (rev0, 初回)
 ├─ B (rev1, parent=A)   追加ログを足して再解析
 │   └─ C (rev2, parent=B) さらに BQ を足して再解析
 └─ D (rev1, parent=A)   別仮説で分岐
   （B・C・D すべて root_run_id = A）
```

- **保存位置は DB 列**（`result_json` 内には埋めない）。一覧・フィルタ・系譜たどりを
  JSON パースなしで SQL 実行できるため（`list_analysis_history`＝`storage.py:679`）。

---

## 4. バックエンド変更

### 4.1 再解析リクエストの受け口（`ConfigLogRunRequest`＝`api.py:1916`）

```python
# 前回解析の推論サマリ。指定時はこれを「入力」の一部として log_text 先頭に注入する。
prior_reasoning: str | None = None
```

### 4.2 前回サマリを log_text 先頭に注入（`_gen_single`＝`api.py:2194` 付近）

既存の `policy_prefix` / `source_block` を先頭に足しているのと同じパターン。

```python
prior_block = ""
if req.prior_reasoning:
    prior_block = (
        "## 前回の解析結果（要約）\n"
        + req.prior_reasoning.strip()
        + "\n\n上記の前回推論と、以下の追加情報を踏まえ真因を再評価してください。\n\n"
    )
single_log_text = prior_block + policy_prefix["text"] + source_block + single_log_text
```

- 2 段階モード（`_gen_two_stage`＝`api.py:2260`）にも同様の注入が必要か要判断。
  MVP は 1 段階（`single_source`）で回す前提とし、2 段階対応は後続。

### 4.3 入力バリデーションの緩和（`api.py:2015-2035` 付近）

現状「ログ / 設定が 1 件も無い」と 400 で弾かれる。`prior_reasoning` があれば入力ありとみなす。

```python
has_any_log = (... 既存 ...) or has_any_bq or bool(req.prior_reasoning)
```

- 新規ログ / BQ を足した場合は既存の検証がそのまま通る（緩和は「前回推論＋自由記述のみ」の救済）。

### 4.4 追加情報（新規ログ / BQ）

**追加改修なし**。既存の `node_logs` / `node_configs` / `node_bigquery` の口をそのまま使う。

- 新規ログ: `_build_topology_log_text`（`api.py:705`）がノード別に合成 → 前回サマリの後ろに続く。
- 新規 BQ: `node_bigquery` 指定で実行時ライブ取得（`api.py:2007-2018` / `_bq_for`＝`api.py:2114-2119`）。
  **保存不要・毎回取得**なので no-save 方針と整合。

### 4.5 履歴保存に系譜＋ファイル名を追加（`api.py:1089` 保存 EP / `storage.py:631` insert）

保存リクエスト（`AnalysisHistorySaveRequest`）と `insert_analysis_history` に以下を追加:

- `parent_run_id: str | None`（フロントが元エントリの run_id を渡す）
- `root_run_id: str | None`（初回は自分、再解析は親の root を引き継ぐ）
- `revision: int`（親.revision + 1、初回は 0）
- `input_files: list[str]`（対象ファイル名だけ。`request_json` に格納。本文は入れない）

> 保存はフロント主導（`saveAnalysisHistory`＝`ConfigLogAnalysis.tsx:519`）。
> フロントは「元エントリの run_id（親）」と「今回の新 run_id（子）」を両方知っているため、
> 系譜はフロントが値を渡すだけで成立する。実行ロジックには触れない。

### 4.6 一覧レスポンスに系譜列を出す（`list_analysis_history`＝`storage.py:679` / DTO＝`api.py:1136`）

- `storage.py` の一覧 SELECT に `root_run_id, parent_run_id, revision` を追加。
- `AnalysisHistorySummary` DTO に同 3 フィールドを追加。

### 4.7（任意・堅牢版）系譜取得のクエリ

- 一覧 API に「各 root の最新版だけ返す」モードを追加（例 `?collapse=root`、SQL は
  `root_run_id` ごとに `MAX(revision)`）。
- 特定調査の全版取得: `GET /api/analysis-history?root_run_id=<root>`。

---

## 5. フロントエンド変更

### 5.1 再解析の起動（`AnalysisHistoryView.tsx` 詳細）

1. 履歴詳細に「**前回の推論をもとに再解析**」ボタンを追加。
2. 押下時、その履歴の `result_json` から
   `const priorSummary = buildReasoningReport(entry.result)`（既存関数）でサマリ生成。
3. 追加情報の入力欄を出す:
   - 自由記述 → 既存の問診票 UI（`questionnaire_answers`）を流用。
   - 新規ログ / BQ → 既存のノード添付 UI（`nodeLogs` / `nodeBigquery`）を流用。
4. 送信 body は既存の `runOne`（`ConfigLogAnalysis.tsx:607`）とほぼ同じ。差分:

```ts
{
  ...共通,
  prior_reasoning: priorSummary,   // 追加
  node_logs:     { 新規のみ },      // 前回の生ログは送らない
  node_configs:  { 新規のみ },
  node_bigquery: { 新規のみ },
  single_source: 'log',            // or 'both'（新規 config も足すなら）
  questionnaire_answers: { 追加所見 },
}
```

5. 完了後の保存（`saveAnalysisHistory`）で系譜を付与:

```ts
parent_run_id: sourceEntry.run_id,
root_run_id:   sourceEntry.root_run_id ?? sourceEntry.run_id,
revision:      (sourceEntry.revision ?? 0) + 1,
input_files:   collectFileNames(nodeLogs, nodeConfigs),  // 名前だけ
```

### 5.2 履歴一覧のグループ表示＋トグル（`AnalysisHistoryView.tsx` 一覧）

**表示ルール**: 同じ `root_run_id` の版を `revision` 降順に並べ、最新を N とすると

- 既定表示: `revision >= N - 1`（最新＋1 つ前）
- トグルで開閉: `revision <= N - 2`（2 回以上前）→「過去の版を表示」

**実装**:

1. `entries` を `root_run_id` で `Map<root, entry[]>` にグループ化（`useMemo`）。
2. 各グループを `revision` 降順ソート、`maxRev` を算出。
3. `revision >= maxRev - 1` は常時表示、それ未満は開閉。

```ts
const [expandedRoots, setExpandedRoots] = useState<Set<string>>(new Set())
```

一覧イメージ:

```
▼ 調査A（対象: fw-syslog.log ほか）
   ● v3 (最新)  2026-07-27 14:10  確信度0.71  真因: MTU不整合
   ○ v2         2026-07-27 13:40  確信度0.62  真因: BGPフラップ
   ▸ 過去の版 (2件) を表示            ← 押すと v1・v0
▼ 調査B（対象: sw-l2-01 BQ:syslog_tbl）
   ● v1 (最新)  2026-07-26 18:00  確信度0.55  ...
```

- 行クリックは従来どおり `openDetail(id)`（既存の再現ビューを流用）。

### 5.3（任意）詳細の版タイムライン

調査を開いたら上部に `v0→v1→v2` のタブ / スライダー。確信度推移や
「前版との真因候補の差分」を表示（`parent_run_id` で前版を引く）。

---

## 6. 実装ステップ（推奨順）

1. **DB 3 列追加** + 既存行の移行（`root_run_id=run_id`, `revision=0`）。
2. **保存経路**に系譜＋`input_files` を通す（`api.py:1089` / `storage.py:631` / DTO）。
3. **一覧に系譜列を出す**（`storage.py:679` SELECT / `api.py:1136` DTO）。
4. **`prior_reasoning` 受け口＋注入＋バリデーション緩和**（`api.py:1916` / `2194` / `2015`）。
5. **フロント: 再解析ボタン＋追加情報 UI**（詳細画面）。
6. **フロント: 一覧グループ化＋トグル**。
7. （任意）堅牢版の系譜取得 API / 詳細タイムライン / 2 段階モード対応。

---

## 7. 注意点 / 未決事項

- **件数上限**: 一覧は `limit=200` の一括取得（`AnalysisHistoryView.tsx:94`）。再解析で版が
  増えると古い版が枠を食う。デモ規模は MVP（クライアント側グループ化）で可。件数増を
  見込むなら堅牢版（最新のみ取得＋展開時に `?root_run_id=` で遅延取得）にする。
- **中間版の削除**: 鎖が切れる。削除時に子の `parent_run_id` を祖父へ付け替える等のルールを決める。
- **2 段階モード**: MVP は 1 段階前提。`_gen_two_stage`（`api.py:2260`）への注入要否は後続で判断。
- **精度の限界**: 前回推論＋追加情報が上限（メモ [[project_accuracy_cost_2026-07-01]] の data-bound 制約と同性質）。
- **観測性（任意）**: 再解析時に Langfuse trace を親 trace / 同一 session に紐づけると UI 上でも系譜を追える。

---

## 8. 工数まとめ

| 変更 | 箇所 | 規模 |
|---|---|---|
| DB 3 列追加＋移行 | `storage.py:239-262` ＋ ALTER TABLE | 小 |
| 保存に系譜＋`input_files` | `api.py:1089` / `storage.py:631` / DTO | 小 |
| 一覧に系譜 3 列を出す | `storage.py:679` / `api.py:1136` | 小 |
| `prior_reasoning` 受け口＋注入＋検証緩和 | `api.py:1916` / `2194` / `2015` | 小 |
| 追加情報（新規ログ / BQ） | 既存の口を流用 | なし |
| 再解析ボタン＋追加情報 UI | `AnalysisHistoryView.tsx` 詳細 | 中 |
| 一覧グループ化＋トグル | `AnalysisHistoryView.tsx` 一覧 | 中 |
| （任意）堅牢版取得 / 版タイムライン / 2 段階対応 | 一覧 API・詳細・`_gen_two_stage` | 小〜中 |

**新エンドポイント・新解析ロジック・DB スキーマの作り直しは不要**（`result_json` と
`buildReasoningReport` を流用し、既存の実行パイプラインをそのまま再利用するため）。
