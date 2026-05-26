"""構成4 統合ノード。

各監視エージェントの findings を共通スキーマ ``AnalysisResult`` 形に変換する
最終段。`human_judgment_required: true` を一度立てたら下げない（議事録 L3）。
"""
from __future__ import annotations

import json
import os
import time

import anthropic

from log_analyzer.rally._helpers import safe_extract_json
from log_analyzer.rally.state import Config4State

INTEGRATOR_PROMPT = """\
あなたは構成4 ラリー型システムの最終統合エージェントです。
委譲チェーンを通過した各監視エージェント (FW / Routing / App / DNS / Sec) の findings と
委譲履歴を受け取り、共通スキーマ AnalysisResult の中身
（root_cause_candidates, recommended_actions, confidence）を構築してください。

統合ルール:
- 複数監視で支持された原因を配列先頭に。1 監視のみが言うものは後方に
  ※候補同士は並列扱い (UI 上もランキングではなくフラット表示)。「rank 1」の概念は撤去
- recommended_actions: ロールバック・再起動・設定変更・データ削除を伴うアクションは
  必ず `human_judgment_required: true`（議事録 L3、外せないフラグ）。
  各監視が立てた true は統合後も維持し、false に上書きしない
- confidence: 監視間で結論が一致する度合いに応じて算出
  - 全監視一致 + トポロジ裏付けあり: 0.9 以上
  - 一部一致: 0.7 〜 0.85
  - 不一致が多い: 0.5 〜 0.7
- evidence: 元ログ行を引用。トポロジ参照を根拠にする場合はその旨を併記

出力 (JSON のみ):
{
  "root_cause_candidates": [
    {"category": "FW|Net|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["..."]}
  ],
  "recommended_actions": [
    {"action": "...", "human_judgment_required": true, "risk_level": "low|mid|high"}
  ],
  "confidence": 0.0
}

トポロジーコンテキストが与えられている場合（"## トポロジー" ブロックがあれば）:
- 上記 JSON に **`suspected_nodes`** フィールド（オブジェクト配列）を **必ず追加**してください
- 旧形式の `suspected_node_ids`（文字列配列）は **使わず**、必ず `suspected_nodes` を使うこと
- 各要素は `{"node_id": "...", "summary": "...", "severity": "primary|secondary|info"}`
    - `node_id`: 提供された topology の node_id のいずれか（それ以外の文字列は無視される）。キーは "id" ではなく必ず "node_id"
    - `summary`: そのノードで何が起こっているかを 1-2 文の日本語で。空文字は禁止
    - `severity`（小文字のみ）:
        - "primary":   障害の直接原因と判断したノード（fix の対象）
        - "secondary": primary の影響で症状が顕在化したノード（被害者側）
        - "info":      関連はあるが障害に直接関与しないノード
- 関与なしと判断した場合は `"suspected_nodes": []` でよいが、フィールド自体は省略しないこと

トポロジー時の出力例（参考）:
```
{
  "root_cause_candidates": [{"category": "FW", "summary": "...", "evidence": ["..."]}],
  "recommended_actions": [{"action": "...", "human_judgment_required": true, "risk_level": "high"}],
  "confidence": 0.85,
  "suspected_nodes": [
    {"node_id": "fw-01", "summary": "policy reload で lb-to-app-01 ルールが欠落し dst=10.0.2.11 が default-deny で落ちている", "severity": "primary"},
    {"node_id": "lb-01", "summary": "app-01 のヘルスチェック (10.0.1.10→10.0.2.11:8080) が連続失敗し DOWN 判定", "severity": "secondary"}
  ]
}
```

ルール:
- 候補は最大 3 件
- フィールド名・enum 値は英語、summary / action の自然文は日本語
- コードフェンスで囲まない
"""


def integrator_node(state: Config4State) -> dict:
    p_overrides = state.get("prompt_overrides", {}) or {}
    m_overrides = state.get("model_overrides", {}) or {}
    # integrator は最終統合で高品質な推論が要るため Sonnet をデフォルト維持。
    # 必要に応じ RALLY_INTEGRATOR_MODEL で Opus 4.7 等に切替可能。
    model = m_overrides.get("integrator") or os.environ.get(
        "RALLY_INTEGRATOR_MODEL", "claude-sonnet-4-5"
    )
    system_prompt = p_overrides.get("integrator", INTEGRATOR_PROMPT)

    # user を 2 ブロックに分割: 元ログ（安定）+ 動的部分（monitor_results / 履歴 / 追加ログ）。
    # 元ログブロックには ephemeral キャッシュを設定する（実行中に変化しないため）。
    # ユーザーが解析中に投入した追加ログは動的ブロック側に入れることで
    # キャッシュ無効化を避けつつ最終統合に確実に反映する。
    log_block = f"## 元ログ\n{state['log_text']}\n"
    # トポロジー解析タブから実行された場合のみ topology_context が入る。
    # 安定ブロックに含めることで cache を維持しつつ、suspected_node_ids 生成のための
    # 「ノード ID 一覧」を LLM に渡す。
    topology_context = state.get("topology_context") or None
    if topology_context:
        log_block += (
            "\n## トポロジー\n"
            "解析対象のネットワーク構成。ログとの対応関係を踏まえ、障害に関与している可能性が"
            "高いノードIDを最終出力の `suspected_node_ids` に列挙してください。\n"
            f"{json.dumps(topology_context, ensure_ascii=False, indent=2)}\n"
        )
    appended_logs = state.get("appended_logs") or []
    appended_text = ""
    if appended_logs:
        appended_text = "## 解析中に追加投入されたログ\n" + "\n\n".join(
            f"### 追加ログ #{i + 1} (round {a.get('round_added', '?')} で投入、source={a.get('source', '?')})\n"
            f"{a.get('content', '')}"
            for i, a in enumerate(appended_logs)
        ) + "\n\n"
    dynamic_payload = {
        "monitor_results": state.get("monitor_results", {}),
        "delegation_history": state.get("delegation_history", []),
        "rally_round_completed": state.get("rally_round", 1),
    }
    dynamic_block = (
        appended_text
        + "## 委譲チェーン結果\n"
        + json.dumps(dynamic_payload, ensure_ascii=False, indent=2)
    )
    user_blocks = [
        {"type": "text", "text": log_block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic_block},
    ]
    user_input = log_block + "\n\n" + dynamic_block  # token_log 保存用

    client = anthropic.Anthropic()
    started = time.perf_counter()
    # 複数ラウンドのラリーで monitor_results が肥大すると応答も長くなりやすい。
    # 切断による JSON parse 失敗を避けるため余裕を持たせる
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_blocks}],
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    raw = response.content[0].text
    parsed, parse_error = safe_extract_json(
        raw,
        fallback={
            "root_cause_candidates": [],
            "recommended_actions": [],
            "confidence": 0.0,
        },
    )
    if parse_error:
        # 後段が info_loss_flags に転記できるようマークしておく
        parsed["_parse_error"] = parse_error
        parsed["_raw_truncated"] = raw[-500:]

    return {
        "result": parsed,
        "token_log_entry": {
            "role": "integrator",
            "model": model,
            "tokens_in": response.usage.input_tokens,
            "tokens_out": response.usage.output_tokens,
            "latency_ms": latency_ms,
            "input": user_input[:2000],
            "raw_output": raw,
        },
    }
