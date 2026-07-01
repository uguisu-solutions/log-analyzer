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
from log_analyzer.tracing import usage_components

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
  - 各アクションに `kind` を付与する:
    - `"provisional"`: 暫定対応（応急処置・早期の症状緩和・回避策）
    - `"permanent"`:   本質対応（根本原因の恒久的な解消）
    可能なら **暫定対応と本質対応の両方**を提示する（少なくとも本質対応は 1 つ以上）
  - 各アクションに次も付与する:
    - `confidence`: そのアクションが妥当である確信度 (0.0–1.0)。**kind ごとに confidence 降順**で並べる
    - `steps`: **ジュニアクラスのエンジニアがそのまま着手できる粒度**の具体手順を順序付き配列で。
      対象機器・コマンド例・確認すべき観点を含め、各要素 1 ステップ
    - `risks`: その手順を実施する際に想定されるリスク・副作用・影響範囲（配列）
    - `rollback_possible`: 作業をロールバックできるか。`"yes"` / `"no"` / `"unknown"` のいずれか
    - `rollback_note`: ロールバック可能な場合の戻し方、不可なら理由・注意（文字列、不要なら空）
- confidence: 「監視間の一致度」と「証拠の直接性」の両方で較正する（自信過剰を避ける）
  - 複数監視が一致 かつ 根拠を元ログ/ソースから直接引用できる: 0.8 〜 0.9
  - 単一監視のみ／単一ラウンドで交差検証が無い、または根拠が推論主体で直接引用が乏しい: 上限 0.7
  - 対抗仮説を十分に除外できていない／真因が提供証拠から直接裏付けられない: 0.5 〜 0.65
  - 「もっともらしいが提供ログ/ソースに直接の裏付けが無い」場合は、断定せず confidence を下げること
- evidence: 元ログ行を引用。トポロジ参照を根拠にする場合はその旨を併記

鑑別診断・証拠規律（重要。人間の熟練解析を模倣する）:
- 各 root_cause_candidate には対抗仮説（他に考えられる原因）を最低 1 つ念頭に置き、
  「なぜ他ではなくこれか（他仮説を退けた理由）」が分かる根拠を summary / evidence に含めること。
  症状に合う根拠だけでなく「他の原因では説明しにくい」根拠も示す。
- evidence には、提供されたログ/ソース抜粋に **実在する記述だけ** を引用する。
  提供証拠に無い具体値（件数・ID・IP・日時・行数）を推測で書かない
  （例:「579 件」「id=91171」等は、実際に提供ログ内に在る場合のみ記載）。
  ログは反復行が「省略サマリ（同種行を集約 ×N 件）」に畳み込まれている場合があり、
  その件数は使ってよいが、原文に無い個別値をでっち上げないこと。
  確認できないが重要な点は summary に「（要確認: 〜）」と明示し、断定しない。
- 真因が提供証拠から直接裏付けられない場合は、無理に断定せず confidence を下げ、
  recommended_actions（kind="provisional"）に
  「原因確定に必要な追加データ（例: 〜のログ/設定/スタックトレース）を取得する」を 1 件加えること。
  正直な「情報不足」は誤った断定より価値が高い。

出力 (JSON のみ):
{
  "root_cause_candidates": [
    {"category": "FW|Net|App|DNS|Sec|Unknown", "summary": "...", "evidence": ["..."]}
  ],
  "recommended_actions": [
    {"action": "...", "human_judgment_required": true, "risk_level": "low|mid|high",
     "kind": "provisional|permanent", "confidence": 0.0,
     "steps": ["手順1 (対象機器/コマンド例/確認観点)", "手順2", "..."],
     "risks": ["想定リスク1", "..."],
     "rollback_possible": "yes|no|unknown", "rollback_note": "..."}
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
  "recommended_actions": [
    {"action": "api-backends 向け permit を一時的に再追加し疎通を回復",
     "human_judgment_required": true, "risk_level": "mid", "kind": "provisional", "confidence": 0.88,
     "steps": ["fw-01 に SSH 接続し `configure terminal` に入る",
               "`access-list inside_out` に api-backends 宛て permit 行を元の位置に再追加",
               "`write memory` で保存し、lb-01 から api-01:443 への疎通を確認"],
     "risks": ["ACL 変更が他通信に波及する可能性", "誤った行追加で別経路を開放するリスク"],
     "rollback_possible": "yes", "rollback_note": "追加した permit 行を削除し write memory すれば原状復帰"},
    {"action": "ACL 変更の承認フロー・構成管理を整備し再発を防止",
     "human_judgment_required": true, "risk_level": "high", "kind": "permanent", "confidence": 0.7,
     "steps": ["構成変更の承認・レビュー手順を文書化", "IaC/構成管理での ACL 管理に移行"],
     "risks": ["運用フロー変更に伴う一時的な作業負荷増"],
     "rollback_possible": "unknown", "rollback_note": ""}
  ],
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
    # config-log 解析の評価方針 (2026-06) で Claude 系ノードは Opus に統一。
    # 必要に応じ RALLY_INTEGRATOR_MODEL で切替可能。
    model = m_overrides.get("integrator") or os.environ.get(
        "RALLY_INTEGRATOR_MODEL", "claude-opus-4-7"
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
    # 推奨アクションに手順/リスク/ロールバックが加わり出力が増えるため余裕を持たせる
    response = client.messages.create(
        model=model,
        max_tokens=6000,
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

    uc = usage_components(response.usage)
    return {
        "result": parsed,
        "token_log_entry": {
            "role": "integrator",
            "model": model,
            # tokens_in は cache 書込/読出を含む入力処理トークン総量
            "tokens_in": uc["input"] + uc["cache_creation"] + uc["cache_read"],
            "tokens_out": uc["output"],
            "cache_creation": uc["cache_creation"],
            "cache_read": uc["cache_read"],
            "latency_ms": latency_ms,
            "input": user_input[:2000],
            "raw_output": raw,
        },
    }
