# 設計プラン：SLM（ローカル小規模モデル）検証対応

> 対象：全構成の LLM 呼び出し層（構成1〜5 ＋ planner / audit / evaluation）。
> 関連：[config_log_stages.md](./config_log_stages.md) / [source_code_analysis.md](./source_code_analysis.md) /
> [reanalysis.md](./reanalysis.md) / [deploy-runbook.md](../deploy-runbook.md)。
>
> **状態（2026-10-02）**: **設計確定・実装未着手**（branch `feature/slm-support`。
> 同ブランチには開発カーネル・ガードレールの整備のみ入っている）。
> 未確定の論点は無い（§12）。**Phase 1 から着手可能**で、Phase 1・2 は GPU もローカルサーバも不要。
>
> **目的は「クラウドをローカルに置き換えること」ではなく「SLM を検証できる状態にすること」**。
> この一点が設計全体の向きを決めている（§0）。

## 0. 目的と非目的

**目的**：**SLM でこのシステムの根本原因分析がどこまで成立するかを検証できる状態にする。**

本システムはもともと「5 構成を同じ入出力契約のまま並列実行・比較する検証プラットフォーム」である。
SLM 対応はその性格をそのまま引き継ぎ、**既存の構成軸に「モデル軸」を足す**。
したがってゴールは「クラウドをローカルに置き換えること」ではなく、
**同じ入力・同じプロンプトでモデルだけを替えて走らせ、結果を突合できること**である。

**非目的（今回やらない）**：

- **クラウドの廃止**。Claude 版は比較対象として残す。既定はクラウドのまま変えない
- **精度の向上**。2026-07-01 の検証で、精度の頭打ちは入力データに正解が無いこと（data-bound）に
  起因すると結論が出ている（[reanalysis.md の「精度の限界」](./reanalysis.md)と同性質）。
  SLM 化は精度を上げない。測るのは**低下幅**である
- **本番運用への切替**。検証結果が出てから別途判断する
- **全ノードの一斉ローカル化**。ノード単位で切り替えられること自体が検証の道具になる（§9）

## 1. 前提と未確定の論点

| 論点 | 状態 |
|---|---|
| 実行環境 | Windows 11 + NVIDIA GeForce RTX 4070 Ti SUPER。vLLM は Windows ネイティブ非対応のため **WSL2 + CUDA** か **llama.cpp / Ollama**（ネイティブ）の二択 |
| **使用可能 VRAM** | **専用 16GB**（RTX 4070 Ti SUPER の仕様値）。タスクマネージャの「GPU メモリ」は専用 16GB ＋ 共有（システム RAM の間借り）の合計表示であり、**共有分は推論に使えない**（WDDM 経由でスピルすると vLLM は OOM、llama.cpp は大幅減速）。念のため確認するなら `nvidia-smi --query-gpu=memory.total --format=csv` |
| システム RAM | 64GB（MoE の expert を CPU オフロードする余地がある） |
| **SLM 化の動機** | **確定（2026-10-02）：SLM の検証**。コスト削減・閉域要件・レイテンシ改善はいずれも目的ではない。したがって **クラウドと SLM を同時に扱えること**（併存・ノード単位の切替・結果の突合）が最優先の要件になる |
| 既定の挙動 | **クラウドのまま**。SLM は環境変数とモデル指定で opt-in（後方互換を壊さない） |

> **重要**：動機が「検証」に確定したことで、ハイブリッド（一部クラウド据え置き）は**妥協ではなく正しい形**になる。
> 「監視だけ SLM」「全部 SLM」を両方走らせて比べることが目的だからである。
> また、**レイテンシとコストは要件ではなく観測対象**になる（遅くても検証は成立する）。
> 一方で「**どの実行がどのモデル構成だったか**」を後から辿れることが必須要件に格上げされる（§9-3）。

## 2. 現状（実装の事実）

### 2-1. LLM 呼び出しは 10 ファイルに直書き（Anthropic 9 / OpenAI 2）

抽象レイヤが無く、各モジュールが `anthropic.Anthropic()` / `openai.OpenAI()` を自前で生成している。
`multi_model_agent.py` は設計上 Anthropic と OpenAI の両方を呼ぶ（構成3 の 3 並列）。

| ファイル | クライアント | max_tokens | cache_control | tool-use |
|---|---|---|---|---|
| [baseline_agent.py](../../apps/agents/src/log_analyzer/baseline_agent.py) | `Anthropic` | 2000 | — | — |
| [filtered_agent.py](../../apps/agents/src/log_analyzer/filtered_agent.py) | `Anthropic` | 800 / 2000 | — | — |
| [multi_model_agent.py](../../apps/agents/src/log_analyzer/multi_model_agent.py) | `AsyncAnthropic` ×2 ＋ `AsyncOpenAI` | 2000 | — | — |
| [pipeline_runner.py](../../apps/agents/src/log_analyzer/pipeline_runner.py) | `AsyncAnthropic` | 2000 | — | — |
| [rally/planner.py](../../apps/agents/src/log_analyzer/rally/planner.py) | `Anthropic` | env | ○ | — |
| [rally/orchestrator.py](../../apps/agents/src/log_analyzer/rally/orchestrator.py) | `Anthropic` | 2000 | ○ | — |
| [rally/monitors.py](../../apps/agents/src/log_analyzer/rally/monitors.py) | `Anthropic` | 8000 | ○ | **○（最大 6 往復）** |
| [rally/integrator.py](../../apps/agents/src/log_analyzer/rally/integrator.py) | `Anthropic` | env | ○ | — |
| [evaluation_agent.py](../../apps/agents/src/log_analyzer/evaluation_agent.py) | `Anthropic` | env(3000) | — | — |
| [audit_agent.py](../../apps/agents/src/log_analyzer/audit_agent.py) | `OpenAI`（Responses API） | — | — | — |

### 2-2. 使っている Anthropic 固有機能

差し替えで変換が必要なのはこの 4 つだけである。

| 機能 | 使用箇所 | ローカルでの対応 |
|---|---|---|
| `system` をブロック配列 ＋ `cache_control: ephemeral` | planner / orchestrator / monitors / integrator（計 7 箇所） | vLLM の Automatic Prefix Caching（**プロンプト側の変更不要**。安定/動的ブロックの分離がそのまま効く） |
| `tools` ＋ `stop_reason == "tool_use"` ＋ `tool_result` ブロック | `monitors._run_monitor_llm` のみ | OpenAI 互換の `tools` / `tool_calls` / `role: "tool"` へ変換（§5） |
| `usage` の 4 分解（`input_tokens` / `cache_creation_input_tokens` / `cache_read_input_tokens` / `output_tokens`） | [tracing.usage_components](../../apps/agents/src/log_analyzer/tracing.py) | `cache_*` は 0 埋め（§8） |
| `response.content[0].text` | 各所（monitors は `_extract_text` で text ブロックのみ連結） | 正規化して文字列で返す（§4） |

### 2-3. モデル指定の経路

`model_overrides`（UI）→ 環境変数 → モジュール既定値、の優先順。

- 環境変数：`BASELINE_MODEL` / `FILTER_MODEL` / `OPENAI_MODEL` / `RALLY_PLANNER_MODEL` /
  `RALLY_ORCHESTRATOR_MODEL` / `RALLY_MONITOR_MODEL` / `RALLY_INTEGRATOR_MODEL` / `AUDIT_MODEL` / `EVAL_MODEL`
- UI の選択肢は [prompt_slots.ANTHROPIC_MODELS](../../apps/agents/src/log_analyzer/prompt_slots.py)
  と `pipeline_runner.ALLOWED_MODELS`（いずれも現在 `["claude-opus-4-7"]`）が正
- 単価表は [tracing._MODEL_PRICES_PER_MTOK](../../apps/agents/src/log_analyzer/tracing.py)

### 2-4. テストの依存

テストは **6 ファイル・22 箇所**で `<module>.anthropic.Anthropic` を monkeypatch している
（`test_rally_orchestrator.py` が 10 箇所で最多）。抽象レイヤを入れると**この差し替え点が移動する**ため、
Phase 1 で同時に移行する必要がある。現状 321 件通過。

### 2-5. JSON 崩れとの既存の戦い

SLM 化で最初に壊れるのはここである。**Opus でも既に対策が入っている**点を押さえる。

- `monitors._retry_if_not_json`：JSON が抽出できなければ「JSON のみ出力せよ」と促して 1 回再生成
- `orchestrator` / `planner`：パース失敗時に 1 回だけ簡潔な再生成
- `rally/_helpers.safe_extract_json`：失敗時は `next: "integrator"` のフォールバック値を返す
  （＝**監視が 1 つスキップされ、精度が静かに落ちる**）
- `monitors._MONITOR_MAX_TOKENS = 8000` のコメントに「小さすぎると最終 JSON が途中で切れて
  parse 失敗 → integrator フォールバックになる」と実測が残っている

## 3. 設計の基本方針

1. **Messages API 互換の薄い内側インタフェースを 1 つ作り、9 箇所をそこへ寄せる**。
   Phase 1 では**振る舞いを一切変えない**（クラウドに対して同じリクエストが飛ぶ）
2. ローカル推論は **OpenAI 互換サーバ**（vLLM / llama.cpp）に寄せる。独自プロトコルは作らない
3. **JSON 崩れは「プロンプトでのお願い」から「サーバ側のスキーマ強制」へ移す**（§6）。
   これは SLM 化の副作用ではなく、**SLM 化で唯一品質が上がる部分**なので積極的に取る
4. prompt caching は prefix caching に読み替える。**プロンプトの構造（安定ブロック＋動的ブロック）は変えない**
5. 既定はクラウド。SLM は `LLM_PROVIDER` で opt-in。**ノード単位で混在できる**こと（ハイブリッド）を要件にする

## 4. 内側インタフェース（新規 `llm_client.py`）

```python
# apps/agents/src/log_analyzer/llm_client.py
@dataclass
class LlmResponse:
    text: str                      # text ブロックを連結した本文
    tool_calls: list[ToolCall]     # 空ならツール要求なし
    stop_reason: str               # "end_turn" | "tool_use" | "max_tokens" | ...
    usage: dict[str, int]          # input / cache_creation / cache_read / output
    raw: Any                       # プロバイダ生応答（Langfuse 用）

def complete(
    *,
    model: str,
    system: str | list[dict],      # 文字列でもブロック配列でも受ける
    messages: list[dict],          # Anthropic 形式をそのまま受ける
    max_tokens: int,
    tools: list[dict] | None = None,
    response_schema: type[BaseModel] | None = None,   # §6
) -> LlmResponse: ...

async def acomplete(...) -> LlmResponse: ...          # multi_model / pipeline_runner 用
```

- **入力の形は Anthropic 準拠**にする。9 箇所の呼び出しを書き換える量を最小にするため
- `model` 文字列の接頭辞でプロバイダを決める（`claude-*` → Anthropic / `gpt-*` → OpenAI /
  `local:*` → OpenAI 互換エンドポイント）。`LLM_PROVIDER` でも全体を上書きできる
- ローカルの接続先は `LOCAL_LLM_BASE_URL`（既定 `http://127.0.0.1:8000/v1`）/ `LOCAL_LLM_MODEL`
- **テストの差し替え点は `llm_client.complete` の 1 箇所**にする（22 → 1 箇所）

> 検討したが採らない案：LangChain / LiteLLM の導入。依存が重く、
> `cache_control` と tool-use ループの制御を取り戻すために結局ラッパを書くことになる。
> 使っている Anthropic 固有機能が §2-2 の 4 つに限られるため、自前の薄い層で足りる。

## 5. tool-use の変換

変換が必要なのは `monitors._run_monitor_llm` のループ 1 箇所のみ。

| Anthropic | OpenAI 互換 |
|---|---|
| `tools: [{name, description, input_schema}]` | `tools: [{type: "function", function: {name, description, parameters}}]` |
| `stop_reason == "tool_use"` | `choices[0].finish_reason == "tool_calls"` |
| assistant の `content` に `tool_use` ブロック | `message.tool_calls[].function.{name, arguments(JSON 文字列)}` |
| user ロールで `tool_result` ブロックを返す | `role: "tool"` ＋ `tool_call_id` |

注意点：

- `arguments` は**文字列**で返るためパースが必要。SLM は壊れた JSON を返すことがあるので、
  パース失敗時はツール結果に「引数が不正」と返して再試行させる（`_MAX_TOOL_ITERATIONS = 6` の内側で回す）
- ツール名は `bigquery_schema` / `bigquery_query` / `source_search` / `source_read` / `db_schema` の 5 つ。
  変換は [rally/tools.py](../../apps/agents/src/log_analyzer/rally/tools.py) と
  [rally/source_tools.py](../../apps/agents/src/log_analyzer/rally/source_tools.py) のスキーマ定義を
  機械変換するだけで済む（`input_schema` → `parameters` はキー名の差のみ）

## 6. 構造化出力（SLM 化の成否を決める部分）

### 現状の問題

各ノードの出力スキーマは **プロンプト内の JSON 仕様としてしか存在しない**。Pydantic モデルは
`AnalysisResult`（最終形）しか無く、`monitors` / `orchestrator` / `integrator` の中間出力は
dict をそのまま扱っている。そのため「モデルが形を守る」ことに依存している。

### 方針

1. **中間出力を Pydantic モデルにする**（新規、[schema.py](../../apps/agents/src/log_analyzer/schema.py)）
   - `OrchestratorDecision`（`first_node` / `focus_hint` / `rationale`）
   - `MonitorOutput`（`findings` / `tool_calls_made` / `confidence` / `next` / `focus_hint_for_next` / `rationale`）
   - `IntegratorOutput`（`root_cause_candidates` / `recommended_actions` / `confidence` / `suspected_nodes`）
   - **プロンプト中の JSON 仕様と二重管理にならないよう、プロンプトのスキーマ部分はモデルから生成する**
2. ローカルでは `response_schema` を vLLM の `guided_json`（xgrammar / Outlines）へ渡し、
   **文法で強制**する。クラウドでは従来どおりプロンプト指定（挙動不変）
3. 効果
   - `safe_extract_json` のフォールバック（監視スキップ）が**原理的に発生しなくなる**
   - 前置きの地の文が出なくなるため**出力トークンが減る**（`_MONITOR_MAX_TOKENS` の実効値が下がり、
     ローカルの生成時間がそのまま縮む）
   - `_retry_if_not_json` の再生成が不要になる（コードは後方互換のため残す）

> これは SLM 化と独立に価値がある改善なので、**Phase 3 はクラウドのままでも先行導入できる**。

## 7. コンテキスト予算の見直し

16GB 級の GPU で 32k コンテキストに収める場合、現状の予算は大きすぎる。
**ローカル時のみ既定値を下げる**（クラウド時は現状維持）。

| 箇所 | 環境変数 | 現状 | ローカル時の推奨 |
|---|---|---|---|
| source 1 実行あたり | `RALLY_SOURCE_RUN_BUDGET_CHARS` | 40,000 字 | 16,000 字 |
| source 1 回の read | `RALLY_SOURCE_READ_MAX_CHARS` | 6,000 字 | 4,000 字 |
| BQ 結果 | `RALLY_BQ_RESULT_MAX_CHARS` | 12,000 字 | 6,000 字 |
| BQ 行数 | `RALLY_BQ_MAX_ROWS_IN_CONTEXT` | 200 行 | 80 行 |
| ログ圧縮の原文保持 | `LOG_COMPACT_MAX_EXAMPLES` | 3 件 | 2 件 |
| 監視の出力上限 | （定数 `_MONITOR_MAX_TOKENS`） | 8,000 | 3,000〜4,000（§6 とセット。要 env 化） |

加えて `monitors._build_user_blocks` が動的ブロックに入れている
**「過去の監視結果（参考）」の全文再送**が累積の主因になる。`focus_hint_for_next` の引き継ぎに寄せ、
全文は落とすことを検討する（**精度に影響するため単独の検証が必要。Phase 5 で扱う**）。

## 8. 観測とコスト

- `tracing.usage_components` はそのまま使える。ローカルは `cache_creation` / `cache_read` が無いので 0 を返す
- `tracing.cost_usd` は未収載モデルで `None` を返す既存挙動を利用し、**ローカルはコスト非表示**にする
  （0 円と表示すると「無料」と誤読されるため。GPU 時間は別軸）
- `prompt_slots.ANTHROPIC_MODELS` / `pipeline_runner.ALLOWED_MODELS` にローカルモデルを追加し、
  **UI からノード単位で切り替えられる**ようにする（ハイブリッド検証の operation がこれで回る）
- Langfuse はモデル名が変わるだけで従来どおり動く

## 9. 検証の形（モデル軸の追加）

動機が「検証」なので、ここが設計の中心になる。**モデルを替えて走らせ、結果を突合する**ための
仕組みを揃える。

### 9-1. 切替機構は既にある

調査の結果、**ノード単位のモデル切替は API 側に既に通っている**。

- `model_overrides: dict[slot_id, model]` が `RunRequest` / `TopologyRunRequest` /
  `ConfigLogRunRequest` の **3 経路すべて**に存在し、各エージェントの
  `m_overrides.get(slot_id)` まで届いている（§2-3）
- 制限しているのは [prompt_slots.ANTHROPIC_MODELS](../../apps/agents/src/log_analyzer/prompt_slots.py)
  と `pipeline_runner.ALLOWED_MODELS` が `["claude-opus-4-7"]` である点のみ。
  `api._validate_overrides` がこのリストで弾いている

つまり**新しい切替 UI は不要**で、許可リストにローカルモデルを足すだけでノード別の混在が動く。

### 9-2. 検証プロファイル（名前付きのモデル構成）

ただし構成4 は slot が 7 つ（orchestrator / 監視 5 / integrator）あり、
**毎回 7 つ分を手入力するのは検証運用に耐えない**。名前付きの組み合わせを用意する。

| プロファイル | 構成 | 検証したいこと |
|---|---|---|
| `cloud`（基準） | 全ノード Opus 4.7 | 比較の基準線。**必ず同じ入力で取り直す** |
| `monitors-local` | 監視 5 つのみローカル、他は Opus | tool-use ＋ 長文 ＋ 日本語の主戦場を SLM が持てるか。**ここが最大の分岐点** |
| `front-local` | planner / orchestrator のみローカル | 出力が小さいノードは SLM で十分か（失敗時フォールバックが既にある） |
| `all-local` | 全ノードローカル（評価役を除く） | 完全ローカルの到達点。integrator の複雑なスキーマが崩れるかを見る |

`evaluation_agent`（採点役）は**全プロファイルで Opus 固定**とする。
審判をローカル化すると比較そのものが成立しないため。`audit_agent` も基準を揃えるため GPT-5.5 固定。

> 実装は「保存済み構成」（`saved_configs` テーブル。`model_overrides_json` を既に持つ）を
> そのまま使える。新テーブルは不要。

### 9-3. 記録が足りない（要対応）

**検証の前提として、ここが今の実装の穴である。**

| テーブル | モデル情報 | 問題 |
|---|---|---|
| `run_history` | **列が無い** | 実行履歴タブでモデル構成で絞り込めない |
| `analysis_history` | **列が無い**（`result_json` の中の `round_metrics[].model` には入っている） | 解析履歴からプロファイル別に並べられない。JSON を開かないと分からない |
| `analysis_evaluations` | `model` 列あり（採点に使ったモデル） | 採点モデルは分かるが、**採点対象がどのモデル構成だったか**は分からない |

対応：両テーブルに `model_profile TEXT`（プロファイル名）と `models_json TEXT`
（`{slot_id: model}` の実績値）を追加する。既存 DB へは `_table_columns` を使った
後付けマイグレーション（`analysis_history` の再解析系譜列と同じ手口）で入れる。

**実績値を記録する**点が重要で、リクエストの指定ではなく「実際に何で動いたか」を残す
（環境変数のフォールバックで別のモデルになっていた、という取り違えを防ぐ）。

### 9-4. 構造化出力は ON/OFF を切替可能にする

§6 の `guided_json` で文法強制すると JSON 崩れは消えるが、
**「SLM が素でどれだけ形を守れるか」という評価軸も消える**。検証目的なので両方測る。

- `LLM_GUIDED_JSON=0/1` で切替。既定 ON（実用側）
- OFF で走らせたときの `safe_extract_json` フォールバック発生回数を記録し、
  「文法強制なしでは監視が何回スキップされたか」を定量化する

### 9-5. モデル候補（専用 VRAM 16GB）

| 順位 | モデル | 置き方 | 備考 |
|---|---|---|---|
| 1 | Qwen3-30B-A3B-Instruct-2507 | llama.cpp Q4_K_M、expert の一部を RAM へ（`--n-cpu-moe`） | 活性 3B のため CPU オフロードの罰則が小さい。RAM 64GB が前提。プリフィルが重いので prefix caching が必須 |
| 2 | Qwen3-14B | Q4_K_M 約 9GB を全部 VRAM、32k | 日本語・ツール・JSON のバランス最良。**最初に試すのはこれ**（全部 VRAM に載り、変数が少ない） |
| 3 | gpt-oss-20b | MXFP4 約 12GB、128k | KV が軽く長文向き。vLLM は Ada 世代で MXFP4 非対応なので llama.cpp 経由 |
| 補助 | Qwen3-8B | 余裕あり | `front-local` プロファイル用 |

複数モデルを**同時常駐はできない**（16GB）。プロファイルを替えるたびにサーバを張り替える運用になる。
`LOCAL_LLM_MODEL` の切替手順を `docs/` に残す。

## 10. フェーズ分割

| Phase | 内容 | 受け入れ条件 |
|---|---|---|
| **1** | `llm_client.py` を新設し 10 箇所を寄せる。**振る舞いは不変**。テスト 22 箇所の差し替え点を `llm_client.complete` へ移行 | `node scripts/check.mjs` が緑（pytest 321 件を維持）。クラウドでの実行結果が従来と一致 |
| **2** | 中間出力の Pydantic 化（§6）。**クラウドのまま**入れる。プロンプトのスキーマ部分をモデルから生成 | 既存の parse フォールバック経路に到達しなくなる。出力トークンの減少を計測 |
| **3** | OpenAI 互換アダプタ（§5 の tool-use 変換）＋ `guided_json` の ON/OFF（§9-4）＋ ローカル起動手順 | ローカルモデルで構成1 が完走し `AnalysisResult` が返る。tool-use は単体テストでカバー |
| **4** | **検証基盤**：許可モデルの追加・検証プロファイル（§9-2）・履歴へのモデル列追加（§9-3） | 同じログを `cloud` と `monitors-local` で走らせ、履歴一覧でプロファイル別に並べられる |
| **5** | **検証の実施**：プロファイル横断の比較、コンテキスト予算の調整（§7）、結果を本ファイルに追記 | 真因到達度（手採点 ＋ `evaluation_agent`）・レイテンシ・フォールバック発生回数を記録 |

順序の理由：Phase 2（構造化出力）を SLM 導入より**前**に置く。クラウドのままで品質が上がり、
かつ SLM 切替時に JSON 崩れが最初から出ないため、Phase 3 の切り分けが楽になる。

**Phase 1・2 は GPU もローカルサーバも不要**で、今すぐ着手できる。

## 11. 変更 / 新規ファイル一覧

| 区分 | ファイル | Phase |
|---|---|---|
| 新規 | `apps/agents/src/log_analyzer/llm_client.py` | 1 |
| 新規 | `apps/agents/tests/test_llm_client.py` | 1 |
| 変更 | `baseline_agent.py` / `filtered_agent.py` / `multi_model_agent.py` / `pipeline_runner.py` / `evaluation_agent.py` / `rally/{planner,orchestrator,monitors,integrator}.py` | 1 |
| 変更 | `tests/test_{evaluation,langfuse_instrumentation,planner,rally_bigquery_tool,rally_orchestrator,source_rally_integration}.py`（モック差し替え点の移行） | 1 |
| 変更 | `schema.py`（`OrchestratorDecision` / `MonitorOutput` / `IntegratorOutput` 追加） | 2 |
| 変更 | `rally/{orchestrator,monitors,integrator}.py`（プロンプトのスキーマ部分をモデルから生成） | 2 |
| 新規 | `llm_client` 内の OpenAI 互換アダプタ（tool-use 変換・`guided_json`） | 3 |
| 変更 | `README.md`（ローカル推論サーバの起動とモデル切替の手順） | 3 |
| 変更 | `prompt_slots.py` / `pipeline_runner.py`（`ALLOWED_MODELS` にローカルモデル追加） | 4 |
| 変更 | `storage.py`（`run_history` / `analysis_history` に `model_profile` / `models_json` を後付け） | 4 |
| 変更 | `api.py`（実績モデルを履歴に渡す）／ `apps/ui/src/RunHistoryView.tsx`・`AnalysisHistoryView.tsx`（プロファイル列・絞り込み） | 4 |
| 変更 | `tracing.py`（ローカルモデルのコスト非表示） | 4 |
| 変更 | `rally/{tools,source_tools}.py` / `log_compaction.py`（予算の既定値） | 5 |

## 12. 決定事項

未確定の論点は無い。以下はすべて決定済みで、**異論があれば差し戻してください**。

| 論点 | 決定 | 理由 |
|---|---|---|
| **目的** | **SLM の検証**（2026-10-02 確定） | クラウド廃止・コスト削減・高速化はいずれも目的ではない。クラウドと SLM の**併存**が要件（§0 / §1） |
| GPU の前提 | **専用 VRAM 16GB** | RTX 4070 Ti SUPER の仕様値。タスクマネージャの「GPU メモリ」は共有メモリ込みの合計で、推論には使えない（§1） |
| Phase の順序 | **抽象化 → 構造化出力 → ローカル接続 → 検証基盤 → 検証** | 構造化出力はクラウドのままで品質が上がり、先に入れると SLM 切替時の切り分けが楽になる（§10） |
| 最初に試すモデル | **Qwen3-14B（Q4_K_M）** | 全部 VRAM に載りオフロードの変数が無いため、最初の検証で原因の切り分けがしやすい（§9-5） |
| 検証の最大の分岐点 | **`monitors-local` プロファイル** | tool-use ＋ 長文 ＋ 日本語の主戦場。ここが通るかで SLM 化の成否が決まる（§9-2） |
| 採点役・監査役 | `evaluation_agent` は **Opus 固定**、`audit_agent` は **GPT-5.5 固定** | 審判をモデル軸に乗せると比較が成立しない（§9-2） |
| integrator | **プロファイルで切替**（`monitors-local` ではクラウド、`all-local` ではローカル） | 検証目的なので「残す / 替える」の両方を測る。出力スキーマが最も複雑なので崩れ方を見る価値がある |
| 構造化出力 | `guided_json` は **ON/OFF 切替可**（既定 ON） | 文法強制すると「素でどれだけ形を守れるか」が測れなくなるため、両方測る（§9-4） |
| レイテンシ | **1 解析あたり数分を許容**する | 検証目的なので要件ではなく観測対象。SSE でラウンドごとに出るため画面は固まらない |
| 構成3 / 構成5 | **比較用として残す**。ローカル時は同時実行数を 1 に制限 | 16GB では複数モデル同時常駐ができず実質シリアル化するが、構成を消すと本来の目的が崩れる |
| 観測 | ローカルは**コスト非表示**（0 円と出さない） | 0 円表示は「無料」と誤読される。GPU 時間は別軸で扱う（§8） |
| 精度の判定基準 | **手採点（Excel「テストケース2」の真因⑥への到達度）が主**、`evaluation_agent` は補助 | 既存の評価方針を変えない。[reanalysis.md](./reanalysis.md) と同じ基準に揃える |
