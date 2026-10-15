# 設計プラン：SLM（ローカル小規模モデル）検証対応

> 対象：全構成の LLM 呼び出し層（構成1〜5 ＋ planner / audit / evaluation）。
> 関連：[config_log_stages.md](./config_log_stages.md) / [source_code_analysis.md](./source_code_analysis.md) /
> [reanalysis.md](./reanalysis.md) / [deploy-runbook.md](../deploy-runbook.md)。
>
> **状態（2026-10-15）**: **Phase 1〜4 完了**。次は Phase 5（Runpod の Pod を起動して検証）。
> 目的を「クラウド LLM と SLM の最終出力の精度差を観測する」に改訂し、**推論方法は同一（違うのはモデルだけ）**とした（§0 / §3-1）。
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

**目的**：**同じ入力に対して、従来のクラウド LLM と SLM の最終出力の精度差を観測できる状態にする**
（2026-10-15 改訂）。

そのための原則は 2 つ：

1. **推論方法は同一**。プロンプト・入力・ツール・処理の流れ・上限値・再試行は、クラウドと SLM で
   完全に同じにする。**違うのはモデルだけ**にしないと、出た差がモデルの差だと言えない（§3-1）
2. **差を観測する仕組みを持つ**。どの実行がどのモデル構成だったかを記録し、同じ入力の実行を
   束ねて並べ、正解（解答シナリオ）に対する採点を比べられるようにする（§9-6）

**クラウド LLM による従来の推論は残す**。既定はクラウドのままで、SLM は明示的に選んだときだけ動く。

本システムはもともと「5 構成を同じ入出力契約のまま並列実行・比較する検証プラットフォーム」である。
SLM 対応はその性格をそのまま引き継ぎ、**既存の構成軸に「モデル軸」を足す**。
したがってゴールは「クラウドをローカルに置き換えること」ではなく、
**同じ入力・同じプロンプトでモデルだけを替えて走らせ、結果を突合できること**である。

**非目的（今回やらない）**：

- **クラウドの廃止**。Claude 版は基準線として残す。既定はクラウドのまま変えない
- **SLM 向けの最適化**。SLM の精度を上げるためにプロンプトや入力を SLM 用に変えることはしない
  （原則 1 に反し、比較が成立しなくなる）
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
2. ローカル推論は Ollama のネイティブ `/api/chat` に寄せる（§4）。独自プロトコルは作らない
3. **推論方法はクラウドと同一**（§3-1）。JSON の形の強制（構造化出力）は**どちらにも掛けない**（§6-1）
4. prompt caching は prefix caching に読み替える。**プロンプトの構造（安定ブロック＋動的ブロック）は変えない**
5. 既定はクラウド。SLM はモデル指定（`ollama:*`）で opt-in。**ノード単位で混在できる**こと（ハイブリッド）を要件にする

### 3-1. 推論方法の同一性（2026-10-15 決定）

| 項目 | クラウド（Opus 4.7） | SLM（Ollama） | 扱い |
|---|---|---|---|
| system / user のプロンプト本文 | 現行 | **同じ文字列** | 変えない。SLM 用の言い換え・短縮はしない |
| 入力（ログ・Config・トポロジ・問診票） | 現行 | **同じ** | SLM だけ入力を削る予算（旧 §7-3）は採らない |
| ツール（名前・説明・スキーマ） | 現行 | **同じ定義を機械変換** | 変換はキー名の差だけ（§5） |
| 処理の流れ（委譲・再試行・フォールバック） | 現行 | **同じコード** | `llm_client` の下だけが違う |
| 出力上限（`max_tokens`） | 各ノードの現行値 | **同じ値**（`num_predict`） | SLM だけ下げない |
| 出力形式の強制 | なし（プロンプト指示のみ） | **なし** | Opus 4.7 は Anthropic の構造化出力の対応モデルに無く、揃えられない。片方だけ掛けると差の原因になる |
| 思考（thinking） | なし（`thinking` 未指定の Opus 4.7 は思考しない） | **`think: false`** | 差を出さないための指定 |
| サンプリング | 既定値（Opus 4.7 は `temperature` 等を受け付けない） | **送らない（モデル既定）** | 片方だけ固定しない |
| prompt caching | `cache_control: ephemeral` | 送らない（Ollama の prefix cache に任せる） | 料金にだけ効き、出力は変わらない |

- 同一性は **Phase 3 のテストで機械的に固定する**：同じ呼び出しを Anthropic 経路と Ollama 経路に通し、
  送る system・messages・tools・上限値が中身として一致することを確かめる
- **残る差はモデル固有のもの**（チャットテンプレート・トークナイザ・ツール呼び出しの書式）。
  これがまさに観測したい差である
- コンテキスト長は SLM 側の `num_ctx` を**入力が全部入る値**にする（§7）。入らなかった場合は
  切り捨てを検知して実行記録に残す（黙って比較に混ぜない）

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

### 4-1. Ollama アダプタの実装メモ（2026-10-15）

- HTTP は**標準ライブラリ（`urllib`）**で送る。httpx は anthropic 経由で入っているが、直接使うと
  依存の追加になるため避けた。非同期版は `asyncio.to_thread` で同期版を包む
- テストの差し替え点は `llm_client._ollama_post`。送受信そのものは localhost の擬似サーバで確かめる
- 同一性（§3-1）は `tests/test_llm_client_parity.py` で固定：`llm_client` 単体の呼び出しと、
  監視ノードの tool-use ループ全体の両方で、全往復のリクエスト中身が Anthropic 経路と一致する
- 複数 text ブロックは空行（`"\n\n"`）で連結して 1 つの文字列にする（既存の `_blocks_to_text` と同じ約束）
- Ollama が tool call の ID を返さない場合は振る（履歴で tool_result と対応付けるため）
- 切り捨て検知は「`prompt_eval_count` が `num_ctx` に達した」で判定し、`LlmResponse.input_truncated`
  に立てる。Ollama のプロンプトキャッシュで評価済みトークンが少なく報告される場合に見逃す可能性があるため、
  **Phase 5 の最初に、わざと長い入力を送って実機の挙動を確かめる**
- 環境変数：`OLLAMA_BASE_URL` / `OLLAMA_NUM_CTX`（既定 32768）/ `OLLAMA_TIMEOUT_SEC`（既定 600）

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
   - **実装済み（2026-10-15）**：3 モデルを `schema.py` に追加。候補・アクション・ノード所見は
     既存の `RootCauseCandidate` / `RecommendedAction` / `SuspectedNodeFinding` をそのまま使う
     （**最終契約より厳しい型にしない**。例：`category` は自由文字列のまま）。
     プロンプトの出力例とモデルのフィールドが一致すること、正規化処理の出力がモデルに収まることを
     `test_intermediate_schema.py` で固定した
   - **プロンプト本文は変えない**（2026-10-15 決定）。スキーマ部分の自動生成は行わない。
     クラウドの精度の基準線を動かさないためで、モデルとのずれは上記テストで防ぐ
2. **出力形式の強制（Ollama の `format`）は使わない**（2026-10-15 決定・§3-1）。
   クラウドと SLM のどちらもプロンプトの指示だけで JSON を出させ、崩れたときの救済
   （`_retry_if_not_json`・再生成・`safe_extract_json` のフォールバック）も同じコードを通す
3. モデルの用途は**形の遵守を測ること**に変わる。各ノードの生出力が中間モデルに収まったか、
   フォールバックに落ちたかを実行ごとに記録し、精度差の内訳（「推論を誤った」のか「形を守れず
   ノードが飛んだ」のか）を分けて見られるようにする（§9-6）

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

**決定**：解析ノードは**常に `think: false`**（2026-10-15 改訂）。基準線の Opus 4.7 は `thinking` を
指定しておらず思考なしで動いているため、それに揃える（§3-1）。思考ありの比較は本設計の範囲外とする
（行うならクラウド側も adaptive thinking にして両方を揃えたうえで、別の検証として扱う）。

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

**SLM だけ予算を下げることはしない**（2026-10-15 改訂・§3-1）。入力・出力上限はクラウドと同じ値を使う。
旧案（source / BQ / ログ圧縮の文字数、監視の出力上限、過去の監視結果の全文再送を SLM 時だけ削る）は
取り下げた。SLM にだけ渡る情報が減ると、精度差がモデルの差ではなく入力の差になるため。

代わりに **`num_ctx` を入力が全部入る値まで上げる**（`OLLAMA_NUM_CTX`）。
A40 48GB では Q4 の 26B / 31B の重み（18〜20GB）の残りを KV キャッシュに使える。
どこまで上げられるかは Phase 5 の最初に実測し、決めた値を本節に追記する。
それでも入らない入力は `prompt_eval_count` で切り捨てを検知し、その実行に印を付けて記録する（§8）。

## 8. 観測とコスト

- `tracing.usage_components` はそのまま使える。Ollama の `/api/chat` が返す
  `prompt_eval_count` → `input`、`eval_count` → `output` に写す。`cache_creation` / `cache_read` は 0
- **`prompt_eval_count` は切り捨て検知にも使う**（§7-1 の対策 2）。観測のためだけでなく
  「入力が本当に全部入ったか」の確認値として扱う
- `tracing.cost_usd` は未収載モデルで `None` を返す既存挙動を利用し、**ローカルはコスト非表示**にする
  （0 円と表示すると「無料」と誤読されるため。GPU 時間は別軸）
  - **確認結果（2026-10-15）：追加実装は不要**。構成4 は単価未登録モデルの分をコストに含めず
    `info_loss_flags` に `cost_unpriced_models` を残す仕組みが既にある。構成1・2・3・5 はクラウドでも
    コストを計算しておらず常に 0.0 で、SLM だけが無料に見えるわけではない。UI はコストを表示していない。
    `Metrics.cost_usd` を `None` 可にするのは schema の型変更（人の確認が必要）なので行わない
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
| **`all-local-26b`** | 全ノード `gemma4:26b-a4b-it-q4_K_M`（評価役を除く） | **主比較**。クラウドとの最終出力の精度差 |
| **`all-local-31b`** | 全ノード `gemma4:31b-it-q4_K_M`（評価役を除く） | **主比較**。同上（26B との差も見る） |
| `monitors-local` | 監視 5 つのみ SLM、他は Opus | 補助。精度差がどのノードで生じるかの切り分け |
| `front-local` | planner / orchestrator のみ SLM | 補助。同上 |

主比較は **`cloud` 対 `all-local-*`**（2026-10-15 改訂）。混在プロファイルは、差が大きかったときに
原因のノードを絞るための補助として使う。

`evaluation_agent`（採点役）は**全プロファイルで Opus 固定**とする。
審判をローカル化すると比較そのものが成立しないため。`audit_agent` も基準を揃えるため GPT-5.5 固定。

> 実装は「保存済み構成」（`saved_configs` テーブル。`model_overrides_json` を既に持つ）を
> そのまま使える。新テーブルは不要。

**実装済み（2026-10-15）**：`prompt_slots.SLM_MODELS`（26B / 31B / 手元の e4b）を追加し、
上書き可能な全 slot と構成5 のノードで `SELECTABLE_MODELS`（先頭がクラウド＝既定）から選べる。
既定モデルは変えていない。SLM の単価は `tracing` に載せない（§8。CLAUDE.md の「モデル追加時は単価表も更新」
の例外で、0 円表示による「無料」の誤読を避けるため）。運用の流れ：

1. 構成4 の各ノードのモデルを Gemma 4 にしたユーザー定義構成を `POST /api/configs/saved` で保存
   （例：`all-local-26b`）。構成の編集画面（BuiltinConfigCanvas）は「単一実行」タブ内にあり、現在非表示
2. `scripts/compare_slm.py --configs config4,user:N` で実行（`user:N` は `_resolve_run_target` が解決する）
3. 解析履歴にはモデル構成の実績（`local:<タグ>` など）と入力の指紋が残り、比較ビューで並ぶ

> **訂正（2026-10-15）**：当初「構成・ログ解析の画面でその構成を選んで実行」と書いたが誤り。
> 同画面には構成を選ぶ欄が無く、構成4 系の先頭（通常は builtin の構成4）が自動で選ばれる。
> 画面から SLM で実行したい場合は、構成を選ぶ欄の追加が必要（未実装）。

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

### 9-4. 構造化出力は使わない（2026-10-15 改訂）

旧案（`LLM_GUIDED_JSON` で SLM にだけ文法強制、既定 ON）は取り下げた。§3-1 のとおり、
片方だけに掛けると推論方法の差になるため。代わりに**形の遵守を両方で同じ物差しで測る**：

- 各ノードについて「生出力が中間モデル（§6-1）に収まったか」「再生成したか」
  「`safe_extract_json` のフォールバックに落ちたか」を実行ごとに記録する
- これにより、精度差を「推論の誤り」と「形を守れずノードが飛んだ」に分けて見られる
- **実装済み（2026-10-15）**：記録先は既存の `info_loss_flags`（schema を変えないため）。
  構成4 は各ノードの再生成を `format_retry: <役割>[ (round N)][ xN]`、入力の切り捨てを
  `input_truncated: <役割>[ (round N)]` で出す。最終的なフォールバックは従来の
  `parse_error round=…` / `integrator_parse_error: …` / `routing_violations: N` のまま。
  構成1 は `input_truncated: analyze` のみ（再生成の仕組みが無いため）。構成2・3・5 は未対応
  （主比較は構成4・構成1 のため。必要になったら同じ書式で足す）。
  比較ビュー用の件数は `run_profile.format_issues(result)` で数える。
  観測用の値はモデルへの入力（`monitor_results` 等）に入らないことをテストで確かめている

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
- 別アプリでは 31B が出力上限 2,048 で回答を切らした。本アプリの出力上限はクラウドと同じ値
  （監視 8,000 など）を使うので余裕はあるが、切れた場合は `stop_reason == "max_tokens"` で検知して記録する
- **モデルは 1 つずつ動かす**（検証資料の方針に合わせる）。プロファイル間は順番に実行して比較する
- Pod は永続ストレージ無しで起動しているため、起動のたびにモデル取得（約 38GB）からになる。
  **Pod を使う作業（Phase 3 の実機確認と Phase 5）は 1 回にまとめる**
- `gemma4:e4b`（ローカル）は**既に動くので Phase 3 の配線確認（疎通・tool-use）に使う**。
  精度の検証は Runpod の 26B / 31B で行う
- 旧案の `qwen3:14b` は 16GB に収めるための候補だった。Runpod 化で VRAM 制約が外れたため取得しない

### 9-6. 精度差の観測（2026-10-15 追加）

ゴール（§0）に直結する部分。**同じ入力の実行を、モデル構成ごとに並べて比べられる**ようにする。

| 何を | どうやって | 既存の仕組み |
|---|---|---|
| どのモデル構成で動いたか | `model_profile` / `models_json`（実績値）を履歴に記録（§9-3）。**実装済み**（`run_profile.py`）。実績値は実行グラフの各ノードのモデル（2 段階解析は `round_metrics`）から取る。プロファイル名は**実績から機械的に決める**：すべてクラウド＝`cloud`、すべて同じ SLM＝`local:<タグ>`、混在＝`mixed`（§9-2 の名前付きプロファイルは実行時の指定名、こちらは記録上の分類） | 列を追加済み |
| 同じ入力の実行を束ねる | 入力の指紋 `input_fingerprint` を記録する。**実装済み**：解析履歴の保存リクエスト（モデル指定は元々含まれない）から表示用の `view_mode` を除き、正規化して SHA-256 の先頭 16 桁。`config_id` は `user:N` を中身に解決し、元の構成とプロンプトの上書きだけを条件に入れる（モデルだけ替えたユーザー定義構成は元の構成と同じ入力として束ねる）。**入力ファイルは名前で識別**（保存リクエストに本文が無いため。同名で中身の違うファイルは区別できない） | `analysis_history.request_json` に条件は丸ごと残っている |
| 正解に対する精度 | **主：手採点**（Excel「テストケース2」の真因への到達度）。**補助：`evaluation_agent`**（Opus 固定、解答シナリオに対し 0〜10 点） | `answer_scenarios` / `analysis_evaluations` がある |
| 比較表 | 解析履歴に「同じ入力 × プロファイル」の比較ビューを足す。行＝入力、列＝プロファイル、セル＝採点・確信度・主要候補・フォールバック回数。セルから各結果を開いて並べて読める。**API 実装済み**：`GET /api/analysis-history/comparison`（各セルは実行順のリスト。プロファイルは cloud → SLM 単独 → mixed の順。指紋が空の古い履歴は除く） | **UI 実装済み**：「クラウド / SLM 比較」タブ（`ModelComparisonView.tsx`）。セルの各実行から解析履歴の詳細を開ける。UI に自動テストの仕組みが無いため型チェック＋lint で担保 |
| 手採点への受け渡し | 比較表を CSV で書き出す（Excel に貼る形）。**実装済み**：`GET /api/analysis-history/comparison.csv`（1 実行 1 行・UTF-8 BOM 付き・手採点用の空欄 `manual_score` 列） | 新規 |
| 精度以外の補助指標 | フォールバック・再生成の回数（§9-4）、入力の切り捨て（§7-1）、レイテンシ、トークン | 一部は `round_metrics` にある |

- **ばらつきを見込む**。クラウドは Opus 4.7 の既定サンプリング、SLM もモデル既定で動くため、
  同じ入力でも結果が揺れる。**1 つの入力をプロファイルごとに複数回（既定 3 回）流し**、
  プロファイル間の差が回ごとのばらつきより大きいかで判断する。回数は課金に直結するため
  実行時に人が決める
- 一括実行は**新しいスクリプト `scripts/compare_slm.py`**（2026-10-15 実装）。シナリオ × 構成
  （`config4` / 保存済みの `user:N`）× N 回（既定 3）を、**起動中のバックエンドに画面と同じリクエストを
  HTTP で送って**流し、画面と同じ形で解析履歴に保存する（比較 API・CSV にそのまま並ぶ）。
  構成ごとにまとめて流す（Pod でのモデル入れ替えを最小にする）。ラウンド上限の確認には「停止」を返す
  （CLI と同じ。どの構成でも同じ条件）。`--dry-run` で計画だけ表示。1 回の失敗で止めず、最後に失敗を一覧する。
  **課金が発生するため実行は人が行う**（既存の `benchmark_questionnaire.py` は問診票比較専用のため使わない）
- 審判（`evaluation_agent`）は全プロファイルで Opus 固定（§9-2）。審判のモデルを替えると比較が成立しない

## 10. フェーズ分割

| Phase | 内容 | 受け入れ条件 |
|---|---|---|
| **1**（**完了** 2026-10-15） | `llm_client.py` を新設し 10 箇所を寄せる。**振る舞いは不変**。テスト 22 箇所の差し替え点を `llm_client` 内の SDK 生成へ移行 | `node scripts/check.mjs` が緑（pytest 321 → 335 件。既存の期待値は不変）。LLM 経路のテストが無かった構成1・2・3・5 は特性テスト（`test_llm_call_sites.py`）を先に足して固定 |
| **2**（**完了** 2026-10-15） | 中間出力の Pydantic 化（§6-1）。プロンプト本文は不変、出力例との一致をテストで固定 | `check.mjs` 緑（pytest 349 件） |
| **3**（**完了** 2026-10-15） | Ollama アダプタ（`/api/chat`・§5 の tool-use 変換・`num_ctx` 明示・`think:false`・切り捨て検知）＋ **送信内容の同一性テスト（§3-1）** ＋ 起動・SSH トンネル・モデル切替の手順 | 同一性テストが緑。ローカルの `gemma4:e4b` で構成1 が完走し `AnalysisResult` が返る。Runpod（26B / 31B）での実機確認は Phase 5 とまとめて行う |
| ↳ 実機確認 | ローカル Ollama 0.40.4・`gemma4:e4b` で構成1 を実行（2026-10-15） | 完走し `AnalysisResult` が返った（tokens_in 1758 / out 1086、約 64 秒、JSON の読み取り失敗なし）。`/api/ps` の `context_length` = 32768。`metrics.cost_usd` が 0.0 と表示される点は Phase 4 で非表示にする |
| **4**（**完了** 2026-10-15） | **観測基盤**（§9-6）：許可モデルの追加・検証プロファイル・履歴へのモデル列と入力の指紋・形の遵守の記録・比較ビュー・CSV 書き出し・一括実行のプロファイル／回数指定 | 同じログを `cloud` と `all-local-*`（テストでは差し替えたモデル）で走らせ、比較ビューで並べて採点を比べられる |
| **5** | **検証の実施**（Pod 起動）：`num_ctx` の実測、`cloud` 対 `all-local-26b` / `all-local-31b` を複数回、手採点と `evaluation_agent`、必要なら混在プロファイルで切り分け。結果を本ファイルに追記 | 真因到達度の差・ばらつき・形の遵守・切り捨て・レイテンシを記録 |

順序の理由：観測基盤（Phase 4）を検証（Phase 5）の前にすべて揃え、Pod の起動を 1 回で済ませる。

**Phase 1〜4 は Pod 不要**（Phase 3 の配線確認はローカルの `gemma4:e4b`）。
**Runpod の Pod を起動するのは Phase 3 の実機確認と Phase 5 のみ**（課金は起動時間に比例するため 1 回にまとめる）。

## 11. 変更 / 新規ファイル一覧

| 区分 | ファイル | Phase |
|---|---|---|
| 新規 | `apps/agents/src/log_analyzer/llm_client.py` | 1 |
| 新規 | `apps/agents/tests/test_llm_client.py` | 1 |
| 変更 | `baseline_agent.py` / `filtered_agent.py` / `multi_model_agent.py` / `pipeline_runner.py` / `evaluation_agent.py` / `rally/{planner,orchestrator,monitors,integrator}.py` | 1 |
| 変更 | `tests/test_{evaluation,langfuse_instrumentation,planner,rally_bigquery_tool,rally_orchestrator,source_rally_integration}.py`（モック差し替え点の移行） | 1 |
| 変更 | `schema.py`（`OrchestratorDecision` / `MonitorOutput` / `IntegratorOutput` 追加） | 2 |
| 新規 | `tests/test_intermediate_schema.py`（プロンプトの出力例とモデルの一致） | 2 |
| 新規 | `llm_client` 内の Ollama アダプタ（`/api/chat`・tool-use 変換・`num_ctx`・`think`・切り捨て検知）＋ 同一性テスト | 3 |
| 変更 | `README.md`（Ollama の起動・`ollama pull`・SSH トンネル・`OLLAMA_BASE_URL` の切替・実効 ctx の確認手順） | 3 |
| 変更 | `prompt_slots.py` / `pipeline_runner.py`（`ALLOWED_MODELS` にローカルモデル追加） | 4 |
| 変更 | `storage.py`（`run_history` / `analysis_history` に `model_profile` / `models_json` / `input_fingerprint` を後付け） | 4 |
| 変更 | `rally/{orchestrator,monitors,integrator}.py`（形の遵守・フォールバックの記録） | 4 |
| 変更 | `api.py`（実績モデル・指紋を履歴に渡す、比較ビュー・CSV 用 API）／ `apps/ui/src/RunHistoryView.tsx`・`AnalysisHistoryView.tsx`（プロファイル列・絞り込み・比較ビュー） | 4 |
| 新規 | `scripts/compare_slm.py`（シナリオ × 構成 × N 回の一括実行）＋ `tests/test_compare_slm.py` | 4 |
| — | `tracing.py`（ローカルモデルのコスト非表示）→ **不要と判断**（§8） | 4 |

## 12. 決定事項

未確定の論点は無い。以下はすべて決定済みで、**異論があれば差し戻してください**。

| 論点 | 決定 | 理由 |
|---|---|---|
| **目的** | **クラウド LLM と SLM の最終出力の精度差を観測する**（2026-10-15 改訂） | クラウド廃止・コスト削減・高速化はいずれも目的ではない。クラウドの従来の推論は残し、SLM と**併存**させる（§0 / §1） |
| **推論方法** | **クラウドと完全に同一。違うのはモデルだけ**（2026-10-15） | 条件が違うと、出た差がモデルの差だと言えない。同一性はテストで固定する（§3-1） |
| **観測** | 履歴にモデル構成と入力の指紋を記録し、同じ入力 × プロファイルの比較ビューと CSV 書き出しを持つ。同じ入力を複数回流してばらつきと比べる（2026-10-15） | 精度差を「見える形」にするのがゴールのため（§9-6） |
| プロンプトの出力形式 | **本文は変えない**。モデルとの一致はテストで固定（2026-10-15） | クラウドの基準線を動かさない（§6-1） |
| GPU の前提 | **検証は Runpod A40 48GB**（2026-10-15 改訂）、配線確認はローカル 16GB | 16GB では 14B 級が上限だったが、Runpod で 26B / 31B を Q4 で載せられる（§1 / §9-5） |
| **推論基盤** | **Ollama**（2026-10-02 確定。ローカル 0.34.2 / Runpod 0.40.3） | Windows ネイティブで動き、Runpod でも同じ API。vLLM は当面採らない。将来はプロバイダ追加で差し替え可（§4） |
| **Runpod への接続** | **SSH トンネル**（`localhost:11435` → Pod の `127.0.0.1:11434`）。`OLLAMA_BASE_URL` で切替、認証ヘッダは持たない | Ollama は無認証のため HTTP ポートを公開しない（§1） |
| **Runpod に送るデータ** | **合成ログのみ** | Runpod は外部クラウドのため実ログ・顧客データは送らない（§1） |
| **呼び出す API** | Ollama の**ネイティブ `/api/chat`**（OpenAI 互換 `/v1` は使わない） | `/v1` では `num_ctx` を変えられず、既定 4,096 のまま**長いログが黙って切られる**（実機確認済み・§7-1） |
| コンテキスト長 | `options.num_ctx` を**毎回明示**（`OLLAMA_NUM_CTX`、既定 32768）＋ `prompt_eval_count` で切り捨て検知 | 黙って切られると「SLM は精度が出ない」という誤った結論になる（§7-1） |
| thinking | 解析ノードは**常に `think: false`**（2026-10-15 改訂） | 基準線の Opus 4.7 は思考なしで動いているため揃える。思考は出力トークンも食い切る（実機で 2 フィールドの JSON に 477 トークン消費）（§6-2） |
| サンプリング | SLM 側も `temperature` 等を**送らない**（モデル既定） | Opus 4.7 はサンプリング指定を受け付けず既定値で動くため、片方だけ固定しない（§3-1） |
| コンテキスト予算 | **SLM だけ下げない**。`num_ctx` を入力が全部入る値にし、入らなければ切り捨てを記録（2026-10-15 改訂） | SLM にだけ渡る情報が減ると入力の差になる（§7-3） |
| Phase の順序 | **抽象化 → 構造化出力 → ローカル接続 → 検証基盤 → 検証** | 構造化出力はクラウドのままで品質が上がり、先に入れると SLM 切替時の切り分けが楽になる（§10） |
| 検証するモデル | **`gemma4:26b-a4b-it-q4_K_M` / `gemma4:31b-it-q4_K_M`（Runpod）**（2026-10-15 改訂。旧案 `qwen3:14b` は取り下げ）。配線確認はローカルの `gemma4:e4b` で行う | 別アプリの検証で tools 対応と Q4 での動作を実測済み。VRAM 48GB に 1 モデルずつ全部載る（§9-5） |
| 主比較 | **`cloud` 対 `all-local-26b` / `all-local-31b`**（2026-10-15 改訂）。混在プロファイルは原因ノードの切り分け用 | ゴールが最終出力の精度差のため（§9-2） |
| 採点役・監査役 | `evaluation_agent` は **Opus 固定**、`audit_agent` は **GPT-5.5 固定** | 審判をモデル軸に乗せると比較が成立しない（§9-2） |
| 構造化出力 | **使わない**（クラウド・SLM とも）。形の遵守は両方で同じ物差しで記録する（2026-10-15 改訂） | Opus 4.7 は Anthropic の構造化出力の対応モデルに無く揃えられない。片方だけ掛けると推論方法の差になる（§6-1 / §9-4） |
| レイテンシ | **1 解析あたり数分を許容**する | 検証目的なので要件ではなく観測対象。SSE でラウンドごとに出るため画面は固まらない |
| 構成3 / 構成5 | **比較用として残す**。ローカル時は同時実行数を 1 に制限 | モデルは 1 つずつ動かすため実質シリアル化するが、構成を消すと本来の目的が崩れる |
| プロファイル間の比較 | **同時並行ではなく順番に実行**して突合する | モデルを 1 つずつ動かす方針のため（§9-5） |
| 観測 | ローカルは**コスト非表示**（0 円と出さない） | 0 円表示は「無料」と誤読される。GPU 時間は別軸で扱う（§8） |
| 精度の判定基準 | **手採点（Excel「テストケース2」の真因⑥への到達度）が主**、`evaluation_agent` は補助 | 既存の評価方針を変えない。[reanalysis.md](./reanalysis.md) と同じ基準に揃える |
