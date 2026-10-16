# プロジェクト
ネットワーク／システムインフラのログを LLM で分析する **マルチエージェント検証プラットフォーム** の PoC。
同じ入出力契約のまま 5 構成（単一 LLM / フィルタ+圧縮 / マルチモデル並列 / 委譲チェーン型ラリー /
ユーザー定義 DAG）を並列実行・比較し、根本原因分析にどの構成が効くかを評価する。
Python 3.11 + FastAPI + SQLite + ローカル FS、UI は React 19 + Vite。観測性は Langfuse（OSS / ローカル）。

起動時にまず `RESUME.md` を読み、現在の状態と次のタスクを把握する。

# 構成
- バックエンドは `apps/agents/src/log_analyzer/`、フロントは `apps/ui/src/`
- `schema.py` の `AnalysisResult` が **全構成共通の出力契約**。構成1〜5 はすべてこの形を返す
- 構成の実装: `baseline_agent.py`(1) / `filtered_agent.py`(2) / `multi_model_agent.py`(3) /
  `rally_agent.py` + `rally/`(4) / `pipeline_runner.py`(5)
- 構成4 は委譲チェーン型。`rally/orchestrator.py` が初手の監視を 1 つ選び、`rally/monitors.py` の
  各監視（fw/routing/app/dns/sec）が分析後に次ノードを指名、`rally/integrator.py` が統合する
- `prompt_slots.py` が「編集可能な slot と許可モデル一覧」の正。UI のモデル選択はここを参照する
- 外部データ取得は `rally/tools.py`（BigQuery）と `rally/source_tools.py`（コードベース / DB スキーマ）。
  いずれも native tool-use のループから呼ばれる
- 永続化は `storage.py`（SQLite: 保存構成 / 実行履歴 / 解析履歴）、ファイルは `filestore.py`
- LLM のトレースとコストは `tracing.py`。モデル単価表もここが正

# 設計書（正）
`docs/plan/*.md` と `README.md` が仕様の正。実装前に該当箇所を読む。
- デプロイ手順は `docs/deploy-runbook.md`
- 設計書と矛盾する実装が必要なら、実装せず理由を報告して指示を待つ
- 仕様を変えたら、同じコミットで該当の docs も直す

# 出力スキーマの契約
UI・`storage.py`・評価エージェントが同じ形に依存しているため、`schema.py` は軽々に変えない。
- フィールドの追加は既定値付き（後方互換）。既存フィールドの意味・型・enum 値は変えない
- `SCHEMA_VERSION` を上げるのは人の判断。勝手に上げない
- プロンプトに書かれた出力形式（`root_cause_candidates` / `recommended_actions` の `kind`・`status`・
  `steps`・`rollback_possible`、`suspected_nodes` 等）は schema と対になっている。
  **片方だけ変えない**
- LLM 出力のパースは `rally/_helpers.py` の `extract_json` / `safe_extract_json` を通す。
  各所で独自にパースを書かない

# モデルと LLM 呼び出し
- モデル統一方針（2026-06）: **Claude 系は Opus 4.7、OpenAI は GPT-5.5**。既定値は各モジュールの
  `_DEFAULT_*_MODEL` と `prompt_slots.py`。環境変数で上書きできる
- 既定モデル・既定プロンプトの変更は精度評価に直結するため人の確認を取る
- 新しいモデルを足すときは `prompt_slots.ANTHROPIC_MODELS` と `tracing._MODEL_PRICES_PER_MTOK`
  の両方を更新する（単価が無いモデルはコスト表示が欠ける）
- prompt caching（`cache_control: ephemeral`）の付け方を変えると入力コストが大きく動く。
  安定ブロックと動的ブロックの分離（`rally/monitors.py` の `_build_user_blocks`）を崩さない

# 開発の進め方（TDD）
1. RED: 失敗するテストを書き、`node scripts/check.mjs backend` で失敗を確認して報告
2. GREEN: そのテストを通す最小実装。通ることを確認して報告
3. REFACTOR: テストが通ったまま整理。`node scripts/check.mjs` を通す
- 実装を先に書かない。テストの無いコードは書かない。1 ケースずつ進める
- コードを変更・追加したら、同じコミットでテストも更新する
- テストを消して通すのは禁止。`skip` を付ける・期待値を実装に合わせて書き換えるのも禁止。実装を直す
- 変更後は必ず `node scripts/check.mjs`（ruff → pytest → tsc → eslint）。通るまで完了と言わない
- テストは `apps/agents/tests/` に置く（`test_<対象>.py`）

# テスト
- **実 LLM API・BigQuery・Langfuse を叩かない**。`apps/agents/tests/conftest.py` が localhost 以外への
  接続を落とす。LLM 呼び出しは `monkeypatch` / `unittest.mock` で差し替える
  （どうしても必要なら `@pytest.mark.allow_network`。付ける前に相談する）
- SQLite の保存先は conftest が一時ディレクトリへ差し替える。実 DB を前提にしない
- 精度の評価は手作業（Excel「テストケース2」との突合）で行う。pytest で精度を測らない
- 実ログ・顧客データをテストに入れない。検証用ログは `samples/logs/` のものを使う

# 秘密情報・禁止
- `.env*`、`secrets/`、`*sa-key.json`、サービスアカウント JSON は読まない・書かない。
  変数を増やすときは `apps/agents/.env.example` に名前だけ（値は人が入れる）
- API キー・トークンをログやコード、docs に出さない
- `.claude/`、`scripts/guard-*.mjs`、`.githooks/`、`.github/workflows/` は編集しない
- `git push`、デプロイ、BigQuery への書き込みは実行しない
- `--no-verify` でフックを飛ばさない
- 実ログは `samples/logs/` の外に置かない（commit ガードで止まる）

# GCP / BigQuery
- プロジェクトは `df-ibc-poc`（東京）。デプロイは人が行う（`docs/deploy-runbook.md`）
- `gcloud` は `describe` / `list` / `config get` などの参照のみ。手順書では PowerShell から
  `& $gc` の形で呼ぶ
- BigQuery は読み取り専用（MCP Toolbox を `writeMode=blocked` で使う）。`bq` CLI からも書き込まない
- 課金が発生するスクリプト（`benchmark_questionnaire.py` / `compare_configs.py` / `compare_slm.py` /
  `ingest_logs_to_bq.py`、`log-analyze` CLI）は人が実行する

# ガードレール
変更を機械的に止める仕組み。壊れていたら作業を止めて報告する。
- `scripts/guard-cmd.mjs` — PreToolUse。危険なコマンドを実行前にブロック
- `scripts/guard-lint.mjs` — PostToolUse。編集したファイルだけ ruff / eslint にかける
- `scripts/guard-commit.mjs` — pre-commit。秘密情報・実行時データ・実ログの混入を止める
- `scripts/test-guard-cmd.mjs` — ガードの回帰テスト。遮断漏れと**誤検知**の両方を固定する。
  赤くなったらガードを緩める前に「本当に通すべき操作か」を確認する
- `scripts/check.mjs` — guard-cmd → ruff → pytest → tsc → eslint をまとめて流す
- 有効化（クローン直後に 1 回）: `git config core.hooksPath .githooks`

# 聞かずに進めてよいこと
- コミットのタイミング・粒度・メッセージ（Conventional Commits、日本語。例: `feat: 〜`・`fix: 〜`・`test: 〜`）
- テスト用ヘルパー・fixture・設定ファイルの作成
- REFACTOR の内容（振る舞いが変わらない範囲）
- 新しい環境変数の追加（既定値を安全側に置き、`.env.example` に名前を足す）
報告は「やったこと」と「check の結果」を最後にまとめて。途中で止まるのは
「人間の確認が必要な操作」に該当するときだけ。

# 人間の確認が必要な操作
`schema.py` の破壊的変更・`SCHEMA_VERSION` の更新 / 既定モデル・既定プロンプトの変更 /
依存パッケージの追加 / prompt caching の設計変更 / GCP へのデプロイ・IAM 変更 /
BigQuery への書き込み / 課金が発生するスクリプトの実行 / ガード（`guard-*.mjs`・`settings.json`）の変更 /
ブランチの push・PR 作成

# ブランチ
- `main` から `<種別>/<内容>` を切って作業する（種別: feature・fix・chore・docs。
  内容は英小文字とハイフン。例: `feature/slm-support`）
- `main` に直接コミットしない

# 報告の形式
各ターンの最後に必ず「次に貼る指示の案」を 1 つ、そのままコピーできる形で書く。
判断が必要な場合は、案を 2〜3 個と、それぞれの違いを 1 行ずつ添える。

# 引き継ぎメモ（RESUME.md）の運用
`RESUME.md` は再開の起点。`.gitignore` 済み（内部の作業状態を含むため公開リポジトリに入れない）。
- 起動時に必ず読む
- 機能を 1 つ終えるたびに該当項目を更新する
- 設計書と違う判断をしたら「設計書からの補完・変更」に 1 行追記し、docs に反映したら消す
- 人に聞くべきことが出たら「未決事項」に追記し、解決したら消す
- セッション終了時（「今日はここまで」と言われたら）現状に合わせて更新する
- 肥大化を防ぐため、完了したものは 1 行に要約し直す。目安は全体で 150 行以内
