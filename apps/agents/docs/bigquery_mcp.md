# BigQuery ログ取得ルート（Google 公式 MCP 経由）

config-log 解析で大容量ログを扱うための **BigQuery 取得ルート**のセットアップと設計。
取得（SELECT）は **Google 公式の BigQuery MCP（[MCP Toolbox for Databases](https://github.com/googleapis/mcp-toolbox)）** をローカルで起動して実行する。

- 決定: 2026-06-10 に BigQuery を新ログ取得ルートとして追加。2026-06-12 に取得実行を MCP 経由へ変更。
- 関連方針: ローカル完結方針からの部分転換（GCP へのログ送信を許容）。AWS は引き続き不使用。

---

## 何が嬉しいか

UI が全文ログを POST → backend が巨大 `log_text` を rally に投入する設計だと、(1) 巨大ファイルで UI フリーズ、(2) 入力トークン肥大で Claude 入力上限に当たる、という問題があった。
事前に BigQuery へログを投入しておき、解析エージェントが **必要なノード/期間/キーワードだけを都度取得** することで両方を解消する。

- ノード単位で「アップロード / BigQuery」を選択できる（既存アップロードルートは温存）。
- BigQuery は **ログ専用**（config は従来どおり inline）。

---

## アーキテクチャ（「裏側だけ MCP」＝処理・UI 不変）

エージェントに見せるツール・host 許可リスト・件数制限・監査用の `bq_evidence` 収集はすべて従来どおり。
**`bigquery_client.py` の実行部だけ** を GCP 直叩きから MCP 呼び出しへ置換している。

```
監視ノード(LLM, native tool-use)
  │  bigquery_schema(host) / bigquery_query(host, 期間, contains, columns…)
  ▼
rally/tools.py  … host 許可リスト検証・既定値補完・行整形
  ▼
bigquery_client.py  … build_query で SELECT 文を組み立て（列名はバッククォート quote）
  │                    dry_run でスキャン量上限を確認
  ▼
bigquery_mcp.py  … toolbox を stdio サブプロセスで常駐起動し execute_sql を呼ぶ
  ▼
toolbox (--config bigquery_toolbox.yaml --stdio)  →  BigQuery
```

- **エージェントは raw SQL を書かない。** `bigquery_query` に host/期間/contains/列を渡すだけで、SQL は `build_query` が必ず `SELECT … LIMIT` を生成する。
- 取得・スキーマ確認・サンプルはすべて **`execute_sql` 1 本**で表現（スキーマは `INFORMATION_SCHEMA.COLUMNS` を SELECT）。
- ログ投入（バルクロード）は MCP 対象外で、`scripts/ingest_logs_to_bq.py` が `google-cloud-bigquery` を直接使う（`ensure_table` / `insert_rows_json`）。

---

## セットアップ

前提: `apps/agents` の venv が作成済み（README のクイックスタート参照）。

### 1. 依存導入（`mcp` クライアント）

```powershell
cd apps\agents
.\.venv\Scripts\Activate.ps1
pip install -e .   # pyproject の mcp>=1.2.0 が入る
```

### 2. toolbox バイナリ（MCP サーバー本体）を入手

Windows (PowerShell):

```powershell
$VERSION = "1.4.0"   # 最新は https://github.com/googleapis/mcp-toolbox/releases で確認
New-Item -ItemType Directory -Force C:\tools | Out-Null
curl.exe -L -o C:\tools\toolbox.exe "https://storage.googleapis.com/mcp-toolbox-for-databases/v$VERSION/windows/amd64/toolbox.exe"
C:\tools\toolbox.exe --version   # 動作確認
```

`.env` の `BIGQUERY_MCP_COMMAND` でフルパス指定すれば PATH 編集は不要。

### 3. 認証・設定（`.env`）

`GOOGLE_APPLICATION_CREDENTIALS`（サービスアカウント JSON のパス）と `BIGQUERY_PROJECT` を
toolbox がそのまま ADC / 対象プロジェクトとして使う。`.env.example` を参照。

---

## 設定リファレンス（`.env`）

| 変数 | 役割 | 既定 |
|--|--|--|
| `GOOGLE_APPLICATION_CREDENTIALS` | SA キー（ADC）。MCP・投入の双方が使用 | — |
| `BIGQUERY_PROJECT` | 対象 GCP プロジェクト | — |
| `BIGQUERY_DATASET` | データセット（取得・`allowedDatasets`） | `network_logs` |
| `BIGQUERY_LOGS_TABLE` | 既定テーブル | `device_logs` |
| `BIGQUERY_LOCATION` | ロケーション | `asia-northeast1` |
| `BIGQUERY_MAX_BYTES_BILLED` | スキャン上限（dry_run で確認） | 1 GiB |
| `BIGQUERY_DEFAULT_LIMIT` / `BIGQUERY_MAX_LIMIT` | 既定/上限 取得件数 | 500 / 2000 |
| `BIGQUERY_MCP_COMMAND` | toolbox 実行コマンド（フルパス可） | `toolbox` |
| `BIGQUERY_MCP_ARGS` | toolbox 起動引数 | `--config ./config/bigquery_toolbox.yaml --stdio` |
| `BIGQUERY_MCP_EXECUTE_TOOL` | SQL 実行ツール名 | `execute_sql` |
| `BIGQUERY_MCP_STARTUP_TIMEOUT` / `BIGQUERY_MCP_CALL_TIMEOUT` | 起動/呼び出しタイムアウト(秒) | 30 / 60 |

> toolbox v1.4.0 の SQL 実行ツール名は **`execute_sql`**（`bigquery-execute-sql` ではない）。
> toolbox は結果を **1 行＝1 text ブロック**（各行 JSON オブジェクト）で返す。

---

## 安全性（多層防御）

| 層 | 効果 |
|--|--|
| ① エージェントのツール面 | LLM は raw SQL を書けない。SQL は `build_query` が必ず `SELECT … LIMIT` を生成。削除を表現する経路が存在しない |
| ② MCP `writeMode: blocked` | DML/DDL（削除含む）を全拒否、SELECT のみ許可 |
| ③ `allowedDatasets` | 指定データセット以外は参照不可（dry_run で範囲外を拒否） |
| ④ 公開ツール最小化 | toolbox の 9 ツール中 `execute_sql` のみ公開 |
| ⑤ IAM（最終防壁・GCP 側） | SA を読み取り専用（例 `roles/bigquery.dataViewer` + `roles/bigquery.jobUser`）にすれば BigQuery 側が削除を拒否 |

ハードニングは [`config/bigquery_toolbox.yaml`](../config/bigquery_toolbox.yaml) の custom config で適用（`--prebuilt` の代わりに `--config` で読ませる）。
project / location / dataset は `${BIGQUERY_*}` の **環境変数補間**で `.env` と同期する
（toolbox は補間に対応。ただし YAML のコメント内に `${...}` を書くと補間されて起動失敗するので注意）。

### writeMode=blocked 実機検証結果（2026-06-12）

実在テーブルに対し、**実行されない安全条件**（`dry_run=true` ＋ `WHERE FALSE`）で検証:

```
DELETE   => BLOCKED (write mode is 'blocked', only SELECT statements are allowed)
UPDATE   => BLOCKED
INSERT   => BLOCKED
MERGE    => BLOCKED
TRUNCATE => BLOCKED
DROP     => BLOCKED
SELECT   => 許可
```

> 注意: 存在しないテーブルに対する破壊文は writeMode 到達前に 404（`allowedDatasets` の事前 dry-run がテーブル不在で失敗）で弾かれる。
> writeMode の効力を確認するときは **実在テーブル**で（上記のとおり安全条件下で）行うこと。

---

## ログ投入（前段・MCP 対象外）

解析の前に、ローカルログを BigQuery へ投入しておく:

```powershell
cd apps\agents
.\.venv\Scripts\Activate.ps1
# 単一ファイル（host 明示）
python scripts\ingest_logs_to_bq.py --host fw-01 ..\..\samples\topology\scenario2_api_acl_missing\fw-01.log
# 日本語(Shift-JIS)ログは --encoding cp932 も可
```

投入はバルクロードのため `google-cloud-bigquery` を直接使う（`ensure_table` でテーブルを idempotent 作成）。

---

## トラブルシュート

| 症状 | 原因 / 対処 |
|--|--|
| `ModuleNotFoundError: mcp` | `pip install -e .` 未実施 |
| 起動タイムアウト / toolbox が見つからない | `BIGQUERY_MCP_COMMAND` のパス、toolbox バイナリの有無を確認 |
| `environment variable not found: "VAR"` | custom config YAML のどこか（コメント含む）に `${...}` が紛れている |
| `invalid tool name: ... does not exist` | `BIGQUERY_MCP_EXECUTE_TOOL` が実ツール名と不一致（v1.4.0 は `execute_sql`） |
| 取得が 0 件 / 結果が空 | 結果は 1 行=1 ブロックで返る。`bigquery_mcp._parse_result` がブロック個別パースする実装か確認 |
| `Illegal input character` 構文エラー | 列名が非 ASCII（日本語等）。識別子はバッククォート quote 必須（`_quote_ident`） |
| `write mode is 'blocked'` | 想定どおり（SELECT 以外を拒否）。投入は `ingest_logs_to_bq.py`（直叩き）で行う |
