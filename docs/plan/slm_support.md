# 設計プラン：SLM（ローカル小規模モデル）検証対応

> 対象：全構成の LLM 呼び出し層（構成1〜5 ＋ planner / audit / evaluation）。
> 関連：[config_log_stages.md](./config_log_stages.md) / [source_code_analysis.md](./source_code_analysis.md) /
> [reanalysis.md](./reanalysis.md) / [deploy-runbook.md](../deploy-runbook.md)。
>
> **状態（2026-10-15）**: **Phase 1 完了**（branch `feature/slm-support`。`llm_client.py` へ集約済み・振る舞い不変）。
> 未確定の論点は無い（§12）。次は Phase 2。Phase 1・2 は GPU もローカルサーバも不要。
>
> **推論基盤は Ollama**（2026-10-02 決定）。実機 0.34.2 に対して
> OpenAI 互換 / 構造化出力 / tool-use / コンテキスト長の挙動を**実測して設計に反映済み**
> （§5 / §6-2 / §7-1 / §9-5）。
>
> **検証の主対象は Runpod 上の Gemma 4 26B A4B / 31B**（2026-10-15 改訂）。
> Runpod の A40（48GB）で Ollama を動かし、SSH トンネルで `localhost:11435` に見せる。
> 手元の Ollama（`localhost:11434`）は配線確認用に残す（§1 / §9-5）。
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
| 実行環境 | **2 系統**（2026-10-15 改訂）。**(a) Runpod（検証の主対象）**：A40 48GB × 1 の Pod で Ollama 0.40.3 を `127.0.0.1:11434` で起動し、ローカル PC から SSH トンネル（`ssh -N -L 11435:127.0.0.1:11434 root@<POD_IP> -p <SSH_PORT>`）で `localhost:11435` に見せる。11434 は外部に公開しない（無認証の API を外へ出さないため）。Pod の準備・トンネル・費用は別リポジトリの検証資料（`slm検証/docs/runpod_gemma4_a6000_validation.md`）に従う。**(b) ローカル（配線確認用）**：Windows 11 + RTX 4070 Ti SUPER の Ollama 0.34.2（`localhost:11434`） |
| **推論基盤** | **Ollama（確定・2026-10-02）**。Runpod 側も Ollama なので API は同じ。vLLM は当面採らない。将来の選択肢としては §4 のプロバイダ差し替えで入れられる |
| **接続先の切替** | `OLLAMA_BASE_URL` 1 本で切り替える（Runpod 時は `http://localhost:11435`）。ローカルと Runpod のモデルを 1 実行の中で混ぜる需要は無いため、モデル別の接続先は持たない |
| **使用可能 VRAM** | Runpod：**48GB**（A40）。ローカル：**専用 16GB**（RTX 4070 Ti SUPER。タスクマネージャの「GPU メモリ」は共有分込みの合計表示で、**共有分は推論に使えない**） |
| システム RAM | ローカル 64GB |
| **データの送り先** | Runpod（外部）にログが渡る。**入れてよいのは合成ログ（`samples/logs/`）のみ。実ログ・顧客データは Runpod に送らない** |
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
| `system` をブロック配列 ＋ `cache_control: ephemeral` | planner / orchestrator / monitors / integrator（計 7 箇所） | Ollama（llama.cpp）のプロンプトキャッシュに読み替え。**プロンプト側の変更は不要**だが、再利用されるのは直前のリクエストと先頭が一致する範囲に限られる。効くのは**同一監視の tool-use ループ内**（往復ごとにプレフィックスが伸びる）で、監視をまたぐ再利用は system が異なるため期待できない（§7-2） |
| `tools` ＋ `stop_reason == "tool_use"` ＋ `tool_result` ブロック | `monitors._run_monitor_llm` のみ | Ollama の `tools` / `message.tool_calls` / `role: "tool"` へ変換（§5。実機確認済み） |
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

- **入力の形は Anthropic 準拠**にする。10 箇所の呼び出しを書き換える量を最小にするため
- `model` 文字列の接頭辞でプロバイダを決める（`claude-*` → Anthropic / `gpt-*` → OpenAI /
  `ollama:*` → Ollama）。`ollama:gemma4:31b-it-q4_K_M` のように**残り全部**を Ollama のモデルタグとして扱う
  （最初のコロンだけで分割する）
- 接続先は `OLLAMA_BASE_URL`（既定 `http://127.0.0.1:11434`。Runpod 検証時は SSH トンネルの
  `http://localhost:11435`）。トンネル経由なので認証ヘッダは持たない
- **Ollama はネイティブ `/api/chat` を使う**（OpenAI 互換 `/v1` ではない）。理由は §7-1 の
  `num_ctx` 問題。`/v1` でもリクエストは通るが**コンテキスト長を変えられず、長いログが黙って切られる**
- 将来 vLLM を足す場合は `openai_compat` プロバイダとして横に追加する（この層の形は変えない）
- **テストの差し替え点は `llm_client` の中の SDK クライアント生成 1 箇所**にする
  （`llm_client.anthropic.Anthropic` / `AsyncAnthropic` / `llm_client.openai.OpenAI` / `AsyncOpenAI`）。
  `llm_client.complete` ごと差し替えるより、**SDK に渡るリクエストの形を既存テストのまま検証できる**ため
  （Phase 1 の受け入れ条件「振る舞い不変」をテストで担保する）。SDK を import してよいのは
  `llm_client.py` だけで、`test_llm_client.py` がこれを固定している
- OpenAI 経路は `gpt-*` のみ。従来の直書きと同じ Responses API（`instructions` / `input` /
  `max_output_tokens` / `reasoning.effort=low` / `text.verbosity=low`）に変換する。
  そのため `AUDIT_MODEL` / `OPENAI_MODEL` に `gpt-` 以外を指定すると Anthropic 経路に入る点に注意

> 検討したが採らない案：LangChain / LiteLLM の導入。依存が重く、
> `cache_control` と tool-use ループの制御を取り戻すために結局ラッパを書くことになる。
> 使っている Anthropic 固有機能が §2-2 の 4 つに限られるため、自前の薄い層で足りる。

## 5. tool-use の変換（実機確認済み）

変換が必要なのは `monitors._run_monitor_llm` のループ 1 箇所のみ。
下表の Ollama 側は **gemma4:e4b で実際に往復を確認した結果**である。

| Anthropic | Ollama `/api/chat` |
|---|---|
| `tools: [{name, description, input_schema}]` | `tools: [{type: "function", function: {name, description, parameters}}]` |
| `stop_reason == "tool_use"` | `message.tool_calls` が存在するか（`done_reason` は `stop` のまま） |
| assistant の `content` に `tool_use` ブロック | `message.tool_calls[].function.{name, arguments}` |
| user ロールで `tool_result` ブロックを返す | `role: "tool"` のメッセージを積む |

注意点：

- **`arguments` の型がエンドポイントで違う**。ネイティブ `/api/chat` は**オブジェクト**で返るため
  パース不要（確認済み: `{"host":"fw-01"}`）。OpenAI 互換 `/v1` は**JSON 文字列**で返る。
  将来 vLLM を足すときは文字列側の処理が必要になるので、アダプタで両対応にしておく
- 壊れた引数が返った場合はツール結果に「引数が不正」と返して再試行させる
  （`_MAX_TOOL_ITERATIONS = 6` の内側で回す）
- **`done_reason` でツール要求を判定しない**。Anthropic の `stop_reason == "tool_use"` に相当する
  信号が無く、`tool_calls` の有無で判定する必要がある
- ツール名は `bigquery_schema` / `bigquery_query` / `source_search` / `source_read` / `db_schema` の 5 つ。
  変換は [rally/tools.py](../../apps/agents/src/log_analyzer/rally/tools.py) と
  [rally/source_tools.py](../../apps/agents/src/log_analyzer/rally/source_tools.py) のスキーマ定義を
  機械変換するだけで済む（`input_schema` → `parameters` はキー名の差のみ）
- ツール対応は**モデルの capabilities 依存**。`/api/tags` の `capabilities` に `tools` が無いモデル
  （導入済みの `gemma3:12b` / `llama3` が該当）は監視ノードに使えない（§9-5）

## 6. モデル出力の扱い（構造化出力 / thinking）

### 6-1. 構造化出力（SLM 化の成否を決める部分）

#### 現状の問題

各ノードの出力スキーマは **プロンプト内の JSON 仕様としてしか存在しない**。Pydantic モデルは
`AnalysisResult`（最終形）しか無く、`monitors` / `orchestrator` / `integrator` の中間出力は
dict をそのまま扱っている。そのため「モデルが形を守る」ことに依存している。

#### 方針

1. **中間出力を Pydantic モデルにする**（新規、[schema.py](../../apps/agents/src/log_analyzer/schema.py)）
   - `OrchestratorDecision`（`first_node` / `focus_hint` / `rationale`）
   - `MonitorOutput`（`findings` / `tool_calls_made` / `confidence` / `next` / `focus_hint_for_next` / `rationale`）
   - `IntegratorOutput`（`root_cause_candidates` / `recommended_actions` / `confidence` / `suspected_nodes`）
   - **プロンプト中の JSON 仕様と二重管理にならないよう、プロンプトのスキーマ部分はモデルから生成する**
2. ローカルでは `response_schema` を Ollama の **`format` に JSON Schema として渡す**（実機確認済み:
   `format` に enum 付きスキーマを与えると、その形の JSON だけが返る）。
   クラウドでは従来どおりプロンプト指定（挙動不変）
3. 効果
   - `safe_extract_json` のフォールバック（監視スキップ）が**原理的に発生しなくなる**
   - 前置きの地の文が出なくなるため**出力トークンが減る**（`_MONITOR_MAX_TOKENS` の実効値が下がり、
     ローカルの生成時間がそのまま縮む）
   - `_retry_if_not_json` の再生成が不要になる（コードは後方互換のため残す）

> これは SLM 化と独立に価値がある改善なので、**Phase 2 はクラウドのままでも先行導入できる**。

### 6-2. thinking 出力の扱い（Ollama 固有・要対応）

導入済みモデルの多くが `thinking` capability を持つ（`deepseek-r1:32b` / `gemma4:e4b`）。
実機で確認した挙動：

- 思考部分は **`content` ではなく別フィールド**に入る（`/v1` では `message.reasoning`、
  ネイティブでは `message.thinking`）。**`content` だけを読むこと**。
  既存の `monitors._extract_text` と同じ発想（text ブロックだけ拾う）をそのまま適用する
- **思考が出力トークンを大量に食う**。2 フィールドだけの JSON を得るのに
  `completion_tokens = 477` を消費した（うち本文は約 20 トークン）。
  `_MONITOR_MAX_TOKENS` を思考が食い切ると**本文が出ないまま打ち切られる**
- **`think: false` で抑止できる**（`/api/chat` / `/v1` 双方で有効・確認済み）

**決定**：解析ノードは既定 `think: false` とし、`LLM_THINK=1` で明示的に有効化できるようにする。
思考を使う検証は「`think` あり / なし」をプロファイルの一軸として別途測る。

## 7. コンテキスト長とコンテキスト予算

### 7-1. `num_ctx` は Ollama 最大の落とし穴（実機確認済み）

| 確認したこと | 結果 |
|---|---|
| Ollama の既定コンテキスト長 | **4,096**（`/api/ps` の `context_length` で確認） |
| `/v1/chat/completions` から `num_ctx` / `options` を渡す | **効かない**。4,096 のまま |
| ネイティブ `/api/chat` の `options.num_ctx` | **効く**。32,768 を指定して反映を確認 |

このアプリは圧縮後でも数千〜数万トークンのログを送るため、**既定の 4,096 では入力が黙って切られる**。
しかも切られたことはレスポンスに現れないので、**「SLM は精度が出ない」という誤った結論になる**。
これが §4 で OpenAI 互換ではなくネイティブ `/api/chat` を採る理由である。

**対策（実装に入れる）**：

1. ローカル呼び出しでは `options.num_ctx` を必ず明示する（`OLLAMA_NUM_CTX`、既定 32768）
2. 呼び出し後に `prompt_eval_count` を記録し、**送ったはずのトークン数と大きく乖離したら警告を出す**
   （切り捨てを黙って通さないための検知）
3. 起動時に `/api/ps` の `context_length` を読み、意図した値かを確認する手順を `README` に残す

### 7-2. プロンプトキャッシュの効き方

Ollama（llama.cpp）のプロンプトキャッシュは**直前のリクエストと先頭が一致する範囲**の再利用である。
Anthropic の `cache_control` とは効く条件が異なる。

- **効く**：同一監視の tool-use ループ内（往復ごとにプレフィックスが伸びるだけなので再利用される）
- **効きにくい**：監視をまたぐ呼び出し（system プロンプトが監視ごとに違うため先頭から不一致）

プロンプト側の構造（安定ブロック＋動的ブロック）は変えない。
将来 vLLM に替えれば Automatic Prefix Caching でまたぎの再利用も効くようになる。

### 7-3. コンテキスト予算

`num_ctx = 32768` に収める前提では、現状の予算は大きすぎる。
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

- `tracing.usage_components` はそのまま使える。Ollama の `/api/chat` が返す
  `prompt_eval_count` → `input`、`eval_count` → `output` に写す。`cache_creation` / `cache_read` は 0
- **`prompt_eval_count` は切り捨て検知にも使う**（§7-1 の対策 2）。観測のためだけでなく
  「入力が本当に全部入ったか」の確認値として扱う
- `tracing.cost_usd` は未収載モデルで `None` を返す既存挙動を利用し、**ローカルはコスト非表示**にする
  （0 円と表示すると「無料」と誤読されるため。GPU 時間は別軸）
- **思考トークンを分けて記録する**（`eval_count` に含まれてしまうため）。`think: false` が既定なので
  通常は 0 だが、thinking あり検証時に「本文に使われたトークン」と区別できないと比較が濁る
- `prompt_slots.ANTHROPIC_MODELS` / `pipeline_runner.ALLOWED_MODELS` にローカルモデルを追加し、
  **UI からノード単位で切り替えられる**ようにする（検証プロファイルの operation がこれで回る）
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

### 9-5. モデル（ローカルの導入状況と Runpod の検証対象）

ローカル実機の `/api/tags` で確認した導入状況（2026-10-02。配線確認用）:

| モデル | パラメータ | 量子化 | サイズ | 宣言 ctx | tools | 本用途での評価 |
|---|---|---|---|---|---|---|
| `deepseek-r1:32b` | 32.8B | Q4_K_M | 19.9GB | 131k | ○ | **16GB に載らない**。CPU スピルで極端に遅くなる。検証の初手には不適 |
| `gemma4:e4b` | 8.0B | Q4_K_M | 9.6GB | 131k | ○ | **現状で唯一そのまま使える**。tool-use / `format` / `think:false` を実機確認済み。ただし 8B 級で監視ノードには小さい |
| `gemma3:12b` | 12.2B | Q4_K_M | 8.1GB | 131k | **×** | tools 非対応のため**監視ノードに使えない**。integrator など非ツールノード専用 |
| `llama3:latest` | 8.0B | Q4_0 | 4.7GB | 8k | **×** | tools 非対応・ctx 8k。本用途では不適 |
| `nomic-embed-text` | 137M | F16 | 0.3GB | 2k | — | 埋め込み専用。本件では未使用 |

**検証の主対象（Runpod・2026-10-15 改訂）**：Gemma 4 の 26B A4B（MoE）と 31B（Dense）を
Q4_K_M で使う。別リポジトリの検証（`slm検証`、2026-10-13、Runpod A40）で次を確認済み。

| モデル（Ollama タグ） | 種別 | サイズ | tools | 別アプリでの実測 |
|---|---|---:|---|---|
| `gemma4:26b-a4b-it-q4_K_M` | MoE（全体 25.2B / 活性 3.8B） | 18GB | ○（日英で確認） | 回答まで中央値 36.2 秒・24 tokens/秒 |
| `gemma4:31b-it-q4_K_M` | Dense 30.7B | 20GB | ○（日英で確認） | 回答まで中央値 48.0 秒・20 tokens/秒。出力上限 2,048 で回答が切れたことが 1 回ある |

- 本アプリでの指定は `ollama:gemma4:26b-a4b-it-q4_K_M` / `ollama:gemma4:31b-it-q4_K_M`
- 上記の正解率（両モデル 100%）は**別アプリ（SQL ツール 1 つ・DuckDB）の結果**であり、
  本アプリ（構成4 は 7 ノード・ツール 5 種・日本語の長文プロンプト）でも同じになる保証は無い。
  それを測るのが本設計の目的である
- 31B で出力が切れた事例があるため、§7-3 の「監視の出力上限 3,000〜4,000」は
  Gemma 4 では下げすぎないこと（切り捨ては `stop_reason == "max_tokens"` で検知して記録する）
- **モデルは 1 つずつ動かす**（検証資料の方針に合わせる）。プロファイル間は順番に実行して比較する
- Pod は永続ストレージ無しで起動しているため、起動のたびにモデル取得（約 38GB）からになる。
  **Pod を使う作業（Phase 3 の実機確認と Phase 5）は 1 回にまとめる**
- `gemma4:e4b`（ローカル）は**既に動くので Phase 3 の配線確認（疎通・tool-use・format）に使う**。
  精度の検証は Runpod の 26B / 31B で行う
- 旧案の `qwen3:14b` は 16GB に収めるための候補だった。Runpod 化で VRAM 制約が外れたため取得しない

## 10. フェーズ分割

| Phase | 内容 | 受け入れ条件 |
|---|---|---|
| **1**（**完了** 2026-10-15） | `llm_client.py` を新設し 10 箇所を寄せる。**振る舞いは不変**。テスト 22 箇所の差し替え点を `llm_client` 内の SDK 生成へ移行 | `node scripts/check.mjs` が緑（pytest 321 → 335 件。既存の期待値は不変）。LLM 経路のテストが無かった構成1・2・3・5 は特性テスト（`test_llm_call_sites.py`）を先に足して固定 |
| **2** | 中間出力の Pydantic 化（§6）。**クラウドのまま**入れる。プロンプトのスキーマ部分をモデルから生成 | 既存の parse フォールバック経路に到達しなくなる。出力トークンの減少を計測 |
| **3** | Ollama アダプタ（`/api/chat`・§5 の tool-use 変換・`format` による構造化出力・`num_ctx` 明示・`think:false`）＋ 起動・SSH トンネル・モデル切替の手順 | ローカルの `gemma4:e4b` で構成1 が完走し `AnalysisResult` が返る。tool-use と切り捨て検知は単体テストでカバー。`/api/ps` の実効 ctx が意図どおり。Runpod（26B / 31B）での実機確認は Phase 5 とまとめて行う |
| **4** | **検証基盤**：許可モデルの追加・検証プロファイル（§9-2）・履歴へのモデル列追加（§9-3） | 同じログを `cloud` と `monitors-local` で走らせ、履歴一覧でプロファイル別に並べられる |
| **5** | **検証の実施**：プロファイル横断の比較、コンテキスト予算の調整（§7）、結果を本ファイルに追記 | 真因到達度（手採点 ＋ `evaluation_agent`）・レイテンシ・フォールバック発生回数を記録 |

順序の理由：Phase 2（構造化出力）を SLM 導入より**前**に置く。クラウドのままで品質が上がり、
かつ SLM 切替時に JSON 崩れが最初から出ないため、Phase 3 の切り分けが楽になる。

**Phase 1・2 は GPU もローカルサーバも不要**で、今すぐ着手できる。
**Runpod の Pod を起動するのは Phase 3 の実機確認と Phase 5 のみ**（課金は起動時間に比例するため 1 回にまとめる）。

## 11. 変更 / 新規ファイル一覧

| 区分 | ファイル | Phase |
|---|---|---|
| 新規 | `apps/agents/src/log_analyzer/llm_client.py` | 1 |
| 新規 | `apps/agents/tests/test_llm_client.py` | 1 |
| 変更 | `baseline_agent.py` / `filtered_agent.py` / `multi_model_agent.py` / `pipeline_runner.py` / `evaluation_agent.py` / `rally/{planner,orchestrator,monitors,integrator}.py` | 1 |
| 変更 | `tests/test_{evaluation,langfuse_instrumentation,planner,rally_bigquery_tool,rally_orchestrator,source_rally_integration}.py`（モック差し替え点の移行） | 1 |
| 変更 | `schema.py`（`OrchestratorDecision` / `MonitorOutput` / `IntegratorOutput` 追加） | 2 |
| 変更 | `rally/{orchestrator,monitors,integrator}.py`（プロンプトのスキーマ部分をモデルから生成） | 2 |
| 新規 | `llm_client` 内の Ollama アダプタ（`/api/chat`・tool-use 変換・`format`・`num_ctx`・`think`・切り捨て検知） | 3 |
| 変更 | `README.md`（Ollama の起動・`ollama pull`・SSH トンネル・`OLLAMA_BASE_URL` の切替・実効 ctx の確認手順） | 3 |
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
| GPU の前提 | **検証は Runpod A40 48GB**（2026-10-15 改訂）、配線確認はローカル 16GB | 16GB では 14B 級が上限だったが、Runpod で 26B / 31B を Q4 で載せられる（§1 / §9-5） |
| **推論基盤** | **Ollama**（2026-10-02 確定。ローカル 0.34.2 / Runpod 0.40.3） | Windows ネイティブで動き、Runpod でも同じ API。vLLM は当面採らない。将来はプロバイダ追加で差し替え可（§4） |
| **Runpod への接続** | **SSH トンネル**（`localhost:11435` → Pod の `127.0.0.1:11434`）。`OLLAMA_BASE_URL` で切替、認証ヘッダは持たない | Ollama は無認証のため HTTP ポートを公開しない（§1） |
| **Runpod に送るデータ** | **合成ログのみ** | Runpod は外部クラウドのため実ログ・顧客データは送らない（§1） |
| **呼び出す API** | Ollama の**ネイティブ `/api/chat`**（OpenAI 互換 `/v1` は使わない） | `/v1` では `num_ctx` を変えられず、既定 4,096 のまま**長いログが黙って切られる**（実機確認済み・§7-1） |
| コンテキスト長 | `options.num_ctx` を**毎回明示**（`OLLAMA_NUM_CTX`、既定 32768）＋ `prompt_eval_count` で切り捨て検知 | 黙って切られると「SLM は精度が出ない」という誤った結論になる（§7-1） |
| thinking | 解析ノードは既定 **`think: false`**（`LLM_THINK=1` で有効化） | 思考が出力トークンを食い切り本文が出ないことがある（実機で 2 フィールドの JSON に 477 トークン消費）。思考あり/なしは検証の一軸として別に測る（§6-2） |
| Phase の順序 | **抽象化 → 構造化出力 → ローカル接続 → 検証基盤 → 検証** | 構造化出力はクラウドのままで品質が上がり、先に入れると SLM 切替時の切り分けが楽になる（§10） |
| 検証するモデル | **`gemma4:26b-a4b-it-q4_K_M` / `gemma4:31b-it-q4_K_M`（Runpod）**（2026-10-15 改訂。旧案 `qwen3:14b` は取り下げ）。配線確認はローカルの `gemma4:e4b` で行う | 別アプリの検証で tools 対応と Q4 での動作を実測済み。VRAM 48GB に 1 モデルずつ全部載る（§9-5） |
| 検証の最大の分岐点 | **`monitors-local` プロファイル** | tool-use ＋ 長文 ＋ 日本語の主戦場。ここが通るかで SLM 化の成否が決まる（§9-2） |
| 採点役・監査役 | `evaluation_agent` は **Opus 固定**、`audit_agent` は **GPT-5.5 固定** | 審判をモデル軸に乗せると比較が成立しない（§9-2） |
| integrator | **プロファイルで切替**（`monitors-local` ではクラウド、`all-local` ではローカル） | 検証目的なので「残す / 替える」の両方を測る。出力スキーマが最も複雑なので崩れ方を見る価値がある |
| 構造化出力 | Ollama の **`format`（JSON Schema）** を使い、**ON/OFF 切替可**（既定 ON） | 実機で enum 付きスキーマの強制を確認済み。常時 ON だと「素でどれだけ形を守れるか」が測れなくなるため両方測る（§6-1 / §9-4） |
| レイテンシ | **1 解析あたり数分を許容**する | 検証目的なので要件ではなく観測対象。SSE でラウンドごとに出るため画面は固まらない |
| 構成3 / 構成5 | **比較用として残す**。ローカル時は同時実行数を 1 に制限 | モデルは 1 つずつ動かすため実質シリアル化するが、構成を消すと本来の目的が崩れる |
| プロファイル間の比較 | **同時並行ではなく順番に実行**して突合する | モデルを 1 つずつ動かす方針のため（§9-5） |
| 観測 | ローカルは**コスト非表示**（0 円と出さない） | 0 円表示は「無料」と誤読される。GPU 時間は別軸で扱う（§8） |
| 精度の判定基準 | **手採点（Excel「テストケース2」の真因⑥への到達度）が主**、`evaluation_agent` は補助 | 既存の評価方針を変えない。[reanalysis.md](./reanalysis.md) と同じ基準に揃える |
