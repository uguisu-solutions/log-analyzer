"""各構成（base_config）が公開する編集可能プロンプトの slot 定義。

Phase 2 W6+ で導入。ユーザー定義構成は「base_config + 一部 slot のプロンプト/モデル上書き」
で表現する。slot 一覧、デフォルトプロンプト、デフォルトモデル、選択可能モデル一覧を返す
API を提供する。

モデル選択:
- 各 slot は Anthropic 系の 3 モデルから選べる（Sonnet 4.5 / Haiku 4.5 / Opus 4.7）。
  config3.analyze だけは 3 モデル並列実行が設計の本質なので model 上書き不可。
- 将来 OpenAI / 他ベンダーを許可する場合は ``ANTHROPIC_MODELS`` を ``allowed_models`` に
  渡すか、slot ごとに別の許可リストを構成する。
"""
from __future__ import annotations

from typing import TypedDict


class SlotInfo(TypedDict):
    slot_id: str
    label: str
    default_prompt: str
    default_model: str
    allowed_models: list[str]


ANTHROPIC_MODELS = [
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-opus-4-7",
]


# slot_id → (表示ラベル, デフォルトモデル, モデル上書き可否) の順序付き定義
# dict 挿入順序で UI に並ぶ（パイプラインの実行順）
SLOT_DEFS: dict[str, dict[str, dict]] = {
    "config1": {
        "analyze": {"label": "分析プロンプト", "default_model": "claude-sonnet-4-5", "model_overridable": True},
    },
    "config2": {
        "triage": {"label": "Triage プロンプト", "default_model": "claude-haiku-4-5", "model_overridable": True},
        "analyze": {"label": "分析プロンプト", "default_model": "claude-sonnet-4-5", "model_overridable": True},
    },
    "config3": {
        # config3.analyze は 3 モデル並列実行が設計の核なのでモデル上書き不可
        "analyze": {"label": "並列モデル共通プロンプト (Sonnet / Haiku / GPT-4o-mini)", "default_model": "(3 モデル並列)", "model_overridable": False},
        "integrate": {"label": "統合プロンプト", "default_model": "claude-sonnet-4-5", "model_overridable": True},
    },
    "config4": {
        # 委譲チェーン型では orchestrator/監視は分類・パターンマッチ寄りタスクなので Haiku を既定に。
        # 最終統合は品質重視で Sonnet を維持。slot 別に UI から上書き可能。
        "orchestrator": {"label": "オーケストレータ", "default_model": "claude-haiku-4-5", "model_overridable": True},
        "fw_monitor": {"label": "FW 監視", "default_model": "claude-haiku-4-5", "model_overridable": True},
        "routing_monitor": {"label": "Routing 監視", "default_model": "claude-haiku-4-5", "model_overridable": True},
        "app_monitor": {"label": "App 監視", "default_model": "claude-haiku-4-5", "model_overridable": True},
        "dns_monitor": {"label": "DNS 監視", "default_model": "claude-haiku-4-5", "model_overridable": True},
        "sec_monitor": {"label": "Security 監視", "default_model": "claude-haiku-4-5", "model_overridable": True},
        "integrator": {"label": "統合", "default_model": "claude-sonnet-4-5", "model_overridable": True},
    },
    # config5（user_pipeline）は slot ベースの上書きを持たない。
    # ノード定義は pipeline_def 全体で UI が直接編集する。
    "config5": {},
}


VALID_BASE_CONFIGS = list(SLOT_DEFS.keys())


def get_default_prompts(base_config: str) -> dict[str, str]:
    """``base_config`` のデフォルトプロンプトを ``{slot_id: prompt}`` で返す。

    各構成の Python モジュールから遅延 import することで循環参照を避ける。
    """
    if base_config == "config1":
        from log_analyzer.baseline_agent import SYSTEM_PROMPT
        return {"analyze": SYSTEM_PROMPT}
    if base_config == "config2":
        from log_analyzer.baseline_agent import SYSTEM_PROMPT
        from log_analyzer.filtered_agent import HAIKU_TRIAGE_PROMPT
        return {"triage": HAIKU_TRIAGE_PROMPT, "analyze": SYSTEM_PROMPT}
    if base_config == "config3":
        from log_analyzer.baseline_agent import SYSTEM_PROMPT
        from log_analyzer.multi_model_agent import INTEGRATION_PROMPT
        return {"analyze": SYSTEM_PROMPT, "integrate": INTEGRATION_PROMPT}
    if base_config == "config4":
        from log_analyzer.rally.integrator import INTEGRATOR_PROMPT
        from log_analyzer.rally.monitors import DEFAULT_MONITOR_PROMPTS
        from log_analyzer.rally.orchestrator import ORCHESTRATOR_PROMPT
        return {
            "orchestrator": ORCHESTRATOR_PROMPT,
            **DEFAULT_MONITOR_PROMPTS,
            "integrator": INTEGRATOR_PROMPT,
        }
    if base_config == "config5":
        # config5 は slot を持たない（ノード定義そのものを編集する）
        return {}
    raise ValueError(f"unknown base_config: {base_config}")


def get_slots(base_config: str) -> list[SlotInfo]:
    """``base_config`` の slot 一覧（ラベル + デフォルトプロンプト + モデル選択肢付き）を返す。

    順序は ``SLOT_DEFS`` の挿入順（パイプラインの実行順に合わせて並べてある）。
    """
    if base_config not in SLOT_DEFS:
        raise ValueError(f"unknown base_config: {base_config}")
    defaults = get_default_prompts(base_config)
    out: list[SlotInfo] = []
    for sid, meta in SLOT_DEFS[base_config].items():
        allowed = ANTHROPIC_MODELS if meta["model_overridable"] else []
        out.append(
            {
                "slot_id": sid,
                "label": meta["label"],
                "default_prompt": defaults.get(sid, ""),
                "default_model": meta["default_model"],
                "allowed_models": allowed,
            }
        )
    return out
