# 設計プラン：SLM（ローカル小規模モデル）対応

> 対象：全構成の LLM 呼び出し層（構成1〜5 ＋ planner / audit / evaluation）。
> 関連：[config_log_stages.md](./config_log_stages.md) / [source_code_analysis.md](./source_code_analysis.md) /
> [deploy-runbook.md](../deploy-runbook.md)。
>
> **状態（2026-10-02）**: **設計のみ。実装未着手**（branch `feature/slm-support`。
> 同ブランチには開発カーネル・ガードレールの整備のみ入っている）。
> **レビュー対象**：§1 の未確定論点と §10 のフェーズ分割。

## 0. 目的と非目的

**目的**：クラウド LLM（Anthropic / OpenAI）への直接依存を、**ローカル SLM に差し替え可能な形**に変える。

**非目的（今回やらない）**：

- **精度の向上**。2026-07-01 の検証で、精度の頭打ちは入力データに正解が無いこと（data-bound）に
  起因すると結論が出ている（[reanalysis.md §9 の「精度の限界」](./reanalysis.md)と同性質）。
  SLM 化は精度を上げない。**同等の精度を維持できるかの検証**が到達目標である
- **モデルの確定**。GPU 構成が未確定のため §9 は候補のままにする
- **全ノードのローカル化**。段階移行を前提とし、ハイブリッド（一部クラウド据え置き）を正とする

## 1. 前提と未確定の論点

| 論点 | 状態 |
|---|---|
| 実行環境 | Windows 11 + NVIDIA GeForce RTX 4070 Ti SUPER。vLLM は Windows ネイティブ非対応のため **WSL2 + CUDA** か **llama.cpp / Ollama**（ネイティブ）の二択 |
| **使用可能 VRAM** | **専用 16GB**（RTX 4070 Ti SUPER の仕様値）。タスクマネージャの「GPU メモリ」は専用 16GB ＋ 共有（システム RAM の間借り）の合計表示であり、**共有分は推論に使えない**（WDDM 経由でスピルすると vLLM は OOM、llama.cpp は大幅減速）。念のため確認するなら `nvidia-smi --query-gpu=memory.total --format=csv` |
| システム RAM | 64GB（MoE の expert を CPU オフロードする余地がある） |
| **SLM 化の動機** | **未確定**（コスト削減 / 閉域・データを外に出さない / レイテンシ）。閉域要件がある場合は `audit_agent` の別ベンダー検証もローカル化が必要になり、§9 の組み方が変わる |
| 既定の挙動 | **クラウドのまま**。SLM は環境変数で opt-in（後方互換を壊さない） |

> **重要**：動機（§12）が未確定でも、**§4〜§8（抽象レイヤ・変換・構造化出力・予算・観測）の設計は変わらない**。
> 未確定に依存するのは §9（ノード割り当て）と Phase 4 の受け入れ条件だけである。
> したがって **Phase 1〜3 は確定を待たずに着手できる**。

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

## 9. モデル候補（GPU 確定後に確定）

### 専用 VRAM 16GB の場合

| 順位 | モデル | 置き方 | 備考 |
|---|---|---|---|
| 1 | Qwen3-30B-A3B-Instruct-2507 | llama.cpp Q4_K_M、expert の一部を RAM へ（`--n-cpu-moe`） | 活性 3B のため CPU オフロードの罰則が小さい。RAM 64GB が前提。プリフィルが重いので prefix caching が必須 |
| 2 | Qwen3-14B | Q4_K_M 約 9GB を全部 VRAM、32k | 日本語・ツール・JSON のバランス最良 |
| 3 | gpt-oss-20b | MXFP4 約 12GB、128k | KV が軽く長文向き。vLLM は Ada 世代で MXFP4 非対応なので llama.cpp 経由 |
| 補助 | Qwen3-8B | 余裕あり | planner / orchestrator / triage 用 |

### 48GB 以上が使える場合

Qwen3-32B（第一候補に昇格）/ Qwen3-30B-A3B を全部 VRAM / Qwen2.5-Bakeneko-32B（日本語重視）。
複数モデルを同時常駐できるため、**構成3 の 3 並列をローカルで本当に並列実行**でき、
`audit_agent` の「別ベンダーで独立検証」もローカル完結できる。

### ノード割り当て（ハイブリッド・初期案）

| ノード | 置き場所 | 根拠 |
|---|---|---|
| planner / orchestrator / config2 triage | ローカル（8B 級） | 出力が小さく、失敗時フォールバックが既にある |
| 5 監視（monitors） | ローカル（14B〜30B 級） | tool-use と長文の主戦場。**ここが通れば SLM 化は成立する** |
| integrator | **クラウド据え置き** | 出力スキーマが最も複雑で、崩れると UI（構成図ハイライト・暫定/本質の分類）がまとめて壊れる |
| audit_agent | クラウド据え置き、または別系統のローカルモデル | 「別ベンダーで独立検証」という設計意図を保つため |
| evaluation_agent | **クラウド据え置き** | 審判をローカル化すると比較評価そのものが成立しない |

## 10. フェーズ分割

| Phase | 内容 | 受け入れ条件 |
|---|---|---|
| **1** | `llm_client.py` を新設し 9 箇所を寄せる。**振る舞いは不変**。テスト 22 箇所の差し替え点を `llm_client.complete` へ移行 | `node scripts/check.mjs` が緑（pytest 321 件を維持）。クラウドでの実行結果が従来と一致 |
| **2** | OpenAI 互換アダプタ（§5 の tool-use 変換を含む）＋ ローカル起動手順を `docs/` に追記 | ローカルモデルで構成1 が完走し、`AnalysisResult` が返る。tool-use は単体テストでカバー |
| **3** | 中間出力の Pydantic 化 ＋ `guided_json`（§6）。**クラウドのままでも先行導入可** | 既存の parse フォールバック経路がテストで到達不能になる。出力トークンの減少を計測 |
| **4** | ノード単位の切替（まず監視のみローカル）＋ Opus 版との比較 | `evaluation_agent` による真因到達度の採点と、手採点（Excel「テストケース2」）での突合 |
| **5** | コンテキスト予算の調整（§7）・ハイブリッド構成の確定 | レイテンシとコストの記録、`docs/plan/` の本ファイルへ結果追記 |

**Phase 1 は GPU 構成の確定を待たずに着手できる**（§1 の注記）。
Phase 4 以降は GPU と動機の確定が前提。

## 11. 変更 / 新規ファイル一覧

| 区分 | ファイル | Phase |
|---|---|---|
| 新規 | `apps/agents/src/log_analyzer/llm_client.py` | 1 |
| 新規 | `apps/agents/tests/test_llm_client.py` | 1 |
| 変更 | `baseline_agent.py` / `filtered_agent.py` / `multi_model_agent.py` / `pipeline_runner.py` / `evaluation_agent.py` / `rally/{planner,orchestrator,monitors,integrator}.py` | 1 |
| 変更 | `tests/test_{evaluation,langfuse_instrumentation,planner,rally_bigquery_tool,rally_orchestrator,source_rally_integration}.py`（モック差し替え点の移行） | 1 |
| 新規 | `llm_client` 内の OpenAI 互換アダプタ（tool-use 変換） | 2 |
| 変更 | `schema.py`（`OrchestratorDecision` / `MonitorOutput` / `IntegratorOutput` 追加） | 3 |
| 変更 | `rally/{orchestrator,monitors,integrator}.py`（プロンプトのスキーマ部分をモデルから生成） | 3 |
| 変更 | `prompt_slots.py` / `pipeline_runner.py`（`ALLOWED_MODELS` にローカルモデル追加） | 4 |
| 変更 | `tracing.py`（ローカルモデルのコスト非表示） | 4 |
| 変更 | `rally/{tools,source_tools}.py` / `log_compaction.py`（予算の既定値） | 5 |
| 変更 | `README.md` / `docs/deploy-runbook.md`（ローカル推論サーバの起動手順） | 2 |

## 12. 決めてほしいこと / こちらで決めたこと

### 決めてほしいこと（1 つだけ）

**なぜローカルで動かしたいのか。** これだけが外からは分からない。
選択によって「クラウド併用を許せるか」が決まり、§9 の割り当てと Phase 4 の受け入れ条件が変わる。

| 選択 | 設計への影響 |
|---|---|
| **A. 料金を下げたい** | §9 のハイブリッドで足りる。integrator・監査・評価は Claude に残す。**最も現実的で、精度の低下幅が最小** |
| **B. データを外に出したくない（閉域）** | 全ノードのローカル化が必要。integrator・`audit_agent`・`evaluation_agent` もローカルになり、精度の低下幅が大きくなる。評価の審判もローカルになるため**比較の基準をどう置くかを別途決める必要がある** |
| **C. 応答を速くしたい** | 16GB のローカル推論は Opus より**遅くなる**ため、この動機では SLM 化を勧めない。別の手（プロンプト短縮・並列化）を提案する |

> A と B は排他ではない。「まず A で始めて、将来 B に備える」なら A として設計し、
> §4 の `llm_client` にノード単位の切替を残しておけば B へ移行できる。

### こちらで決めたこと（異論があれば差し戻してください）

| 論点 | 決定 | 理由 |
|---|---|---|
| GPU の前提 | **専用 VRAM 16GB** として設計する | RTX 4070 Ti SUPER の仕様値。タスクマネージャの「GPU メモリ」は共有メモリ込みの合計で、推論には使えない（§1） |
| Phase の順序 | **1 → 3 → 2** | 構造化出力（§6）は**クラウドのままで品質が上がる**ため、SLM 導入より先に入れる。先に入れておけば SLM 切替時の JSON 崩れが最初から出ない |
| レイテンシ | **1 解析あたり数分を許容**する前提で作る | SSE でラウンドごとに結果が出るため画面は固まらない。許容外と分かった時点で Phase 4 で再判断する |
| 構成3 / 構成5 | **比較用として残す**。ローカル時は同時実行数を 1 に制限する | 単一 GPU では実質シリアル化するが、構成を消すと「5 構成を比較する検証プラットフォーム」という本来の目的が崩れる |
| integrator | **当面クラウド据え置き**（上記 B を選んだ場合のみローカル化） | 出力スキーマが最も複雑で、崩れると UI（構成図ハイライト・暫定/本質の分類）がまとめて壊れる |
| 観測 | ローカルは**コスト非表示**（0 円と出さない） | 0 円表示は「無料」と誤読される。GPU 時間は別軸で扱う（§8） |
