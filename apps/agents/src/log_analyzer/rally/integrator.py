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
この出力の読み手はジュニアエンジニアです。目的は正解の言い当てだけではなく、
「事実確認 → 絞り込み → 根本原因の追求 → 対処支援」という考察の道筋を、
読み手が自分でたどり直し、次の一手を判断できる形で示すことです。

委譲チェーンを通過した各監視エージェント (FW / Routing / App / DNS / Sec) の findings と
委譲履歴を受け取り、共通スキーマ AnalysisResult の中身
（root_cause_candidates, recommended_actions, confidence）を構築してください。

統合ルール:
- 複数監視の evidence で支持された原因を配列先頭に。1 監視のみが言うものは後方に
  ※候補同士は並列扱い (UI 上もランキングではなくフラット表示)。「rank 1」の概念は撤去
- 各候補に `status` を付与する（出力の形を揃えるため、文章で散らさず必ずこの欄で示す）:
  - "supported":  支持された主要候補
  - "secondary":  副次的な要因（主要候補の傍らで寄与しうるが単独では現象を説明しきれない）
  - "rejected":   棄却した仮説（配列から消さず status="rejected" で残す＝黙殺しない）
- 各候補の summary には、原因の説明に加えて「何が確認されればこの候補が確定し、
  何が観測されれば棄却されるか」を 1 文含めること（読み手の次の確認行動を方向づけるため）
- 問診票③「原因の見当」に記入者の見立てが書かれている場合、その見立てを候補として必ず載せ、
  支持なら status="supported/secondary"、否定なら status="rejected" で明示する（黙殺しない）
- recommended_actions:
  - 問診票③「いま一番迷っていること」に記載がある場合、少なくとも 1 つのアクションは
    その迷いに直接回答するものとすること。
    例: 「業務時間中に再起動してよいか判断できない」→ 再起動アクションの steps / risks /
    rollback_possible / rollback_note に、実施可否をその場で判断できる材料
    （影響範囲・所要時間・失敗時の戻し方・実施すべき時間帯の条件）を与える
  - ロールバック・再起動・設定変更・データ削除を伴うアクションは
    必ず `human_judgment_required: true`（議事録 L3、外せないフラグ）。
    各監視が立てた true は統合後も維持し、false に上書きしない
  - 各アクションに `kind` を付与する:
    - "provisional":   暫定対応（応急処置・早期の症状緩和・回避策）
    - "investigation": 調査・切り分け（根本原因の確定に向けた確認手順・観点・結果ごとの分岐）
    - "permanent":     本質対応（根本原因の恒久的な解消）
    可能なら **暫定対応と本質対応の両方**を提示する（少なくとも本質対応は 1 つ以上）。
    根本原因が未確定（confidence が 0.7 未満が目安）の場合は、"investigation" を必ず 1 つ以上含め、
    その steps に「何が出たらどちらに進むか」を書くこと（未確定でも中身を具体化し、空にしない）
  - action / steps の文章には「【調査】」「【暫定】」等の種別ラベルを書かないこと。
    種別は kind 欄で示し、読み手側は見出しで種別を示すため、文中のラベルは重複で不要
  - 各アクションに次も付与する:
    - `confidence`: そのアクションが妥当である確信度 (0.0–1.0)。**kind ごとに confidence 降順**で並べる
    - `steps`: **ジュニアクラスのエンジニアがそのまま着手できる粒度**の具体手順を順序付き配列で。
      対象機器・コマンド例・確認すべき観点を含め、各要素 1 ステップ
    - `risks`: その手順を実施する際に想定されるリスク・副作用・影響範囲（配列）
    - `rollback_possible`: 作業をロールバックできるか。"yes" / "no" / "unknown" のいずれか
    - `rollback_note`: ロールバック可能な場合の戻し方、不可なら理由・注意（文字列、不要なら空）
- confidence（全体）: 監視間で結論が一致した「件数」から機械的に算出しないこと。
  主要候補を支持する evidence の強さと、反証の探索がどこまで行われたか（消したレイヤ・
  棄却した仮説）に基づいて付け、evidence から説明できない数値を付けないこと
- evidence: 元ログ行を引用。トポロジ参照や問診票の記載を根拠にする場合はその旨を併記

出力 (JSON のみ):
{
  "root_cause_candidates": [
    {"category": "FW|Net|App|DNS|Sec|Unknown", "status": "supported|secondary|rejected",
     "summary": "...", "evidence": ["..."]}
  ],
  "recommended_actions": [
    {"action": "...", "human_judgment_required": true, "risk_level": "low|mid|high",
     "kind": "provisional|investigation|permanent", "confidence": 0.0,
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
    - `node_id`: 提供された topology の node_id のいずれか（それ以外の文字列は無視される）。
      キーは "id" ではなく必ず "node_id"
    - `summary`: そのノードで何が起こっているかを 1-2 文の日本語で。空文字は禁止
    - `severity`（小文字のみ）:
        - "primary":   障害の直接原因と判断したノード（fix の対象）
        - "secondary": primary の影響で症状が顕在化したノード（被害者側）
        - "info":      関連はあるが障害に直接関与しないノード
- 関与なしと判断した場合は `"suspected_nodes": []` でよいが、フィールド自体は省略しないこと

トポロジー時の出力例（参考）:
{
  "root_cause_candidates": [{"category": "FW", "status": "supported", "summary": "...", "evidence": ["..."]}],
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

ルール:
- 主要候補（status="supported"）と副次要因（status="secondary"）は合わせて最大 3 件
  （UI 表示互換のため）。棄却した仮説（status="rejected"）はこの 3 件に数えず、
  別枠として残してよい（黙殺しないため）
- フィールド名・enum 値は英語、summary / action の自然文は日本語
- コードフェンスで囲まない
"""


def _integrator_max_tokens() -> int:
    """統合の出力上限。プロンプト改訂で候補の確定/棄却条件・暫定/本質の詳細手順・
    迷いへの回答・suspected_nodes が加わり出力が増えたため既定 10000。旧 6000 では
    途中切断→parse 失敗→候補0/conf0 の空フォールバックが頻発した。
    RALLY_INTEGRATOR_MAX_TOKENS で調整可。"""
    raw = os.environ.get("RALLY_INTEGRATOR_MAX_TOKENS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return 10000


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
    # 推奨アクションに手順/リスク/ロールバックが加わり出力が増えるため余裕を持たせる。
    system = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]
    messages: list[dict] = [{"role": "user", "content": user_blocks}]
    total_in = total_out = total_cc = total_cr = 0

    def _accumulate(usage) -> None:
        nonlocal total_in, total_out, total_cc, total_cr
        c = usage_components(usage)
        total_in += c["input"] + c["cache_creation"] + c["cache_read"]
        total_out += c["output"]
        total_cc += c["cache_creation"]
        total_cr += c["cache_read"]

    response = client.messages.create(
        model=model, max_tokens=_integrator_max_tokens(), system=system, messages=messages
    )
    _accumulate(response.usage)
    raw = response.content[0].text
    parsed, parse_error = safe_extract_json(
        raw,
        fallback={
            "root_cause_candidates": [],
            "recommended_actions": [],
            "confidence": 0.0,
        },
    )
    # パース失敗 (多くは出力切断) → JSON のみで簡潔に 1 回だけ再生成して救済する。
    # 統合が「候補0・conf0」の空フォールバックで完了するのを防ぐ。
    if parse_error:
        messages.append({"role": "assistant", "content": raw or "(空応答)"})
        messages.append({
            "role": "user",
            "content": (
                "前の応答から JSON を抽出できませんでした（途中で切れた可能性があります）。"
                "これまでの分析を踏まえ、指定スキーマ（root_cause_candidates / "
                "recommended_actions / confidence、トポロジ時は suspected_nodes）に厳密に従い、"
                "前置き・説明文・コードフェンスを一切付けず JSON オブジェクトのみを出力してください。"
                "収まらない場合は各アクションの steps / risks を要点に絞って簡潔にすること。"
            ),
        })
        try:
            retry = client.messages.create(
                model=model, max_tokens=_integrator_max_tokens(), system=system, messages=messages
            )
            _accumulate(retry.usage)
            rtext = retry.content[0].text
            retry_parsed, retry_err = safe_extract_json(rtext, fallback=parsed)
            if retry_err is None:
                parsed, parse_error, raw = retry_parsed, None, rtext
        except Exception:  # noqa: BLE001 — 再試行失敗時は初回フォールバックのまま継続
            pass

    if parse_error:
        # 後段が info_loss_flags に転記できるようマークしておく
        parsed["_parse_error"] = parse_error
        parsed["_raw_truncated"] = raw[-500:]

    latency_ms = int((time.perf_counter() - started) * 1000)
    return {
        "result": parsed,
        "token_log_entry": {
            "role": "integrator",
            "model": model,
            # tokens_in は cache 書込/読出を含む入力処理トークン総量
            "tokens_in": total_in,
            "tokens_out": total_out,
            "cache_creation": total_cc,
            "cache_read": total_cr,
            "latency_ms": latency_ms,
            "input": user_input[:2000],
            "raw_output": raw,
        },
    }
