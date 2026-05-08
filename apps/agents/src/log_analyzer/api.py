"""log-analyzer の HTTP API（管理 UI 用 backend）。

起動:
    cd apps/agents
    uvicorn log_analyzer.api:app --reload --port 8000

エンドポイント:
    GET    /api/configs                       構成一覧（builtin + user）
    GET    /api/logs                          samples/logs/ にあるログ一覧
    GET    /api/prompt-slots/{base_config}    base_config のプロンプト slot 定義
    GET    /api/configs/saved                 ユーザー定義構成一覧
    POST   /api/configs/saved                 ユーザー定義構成を新規保存
    PUT    /api/configs/saved/{id}            上書き更新
    DELETE /api/configs/saved/{id}            削除
    POST   /api/runs                          指定構成を指定ログに当て、AnalysisResult を返す
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from log_analyzer import prompt_slots, storage
from log_analyzer.cli import CONFIG_RUNNERS
from log_analyzer.schema import AnalysisResult

load_dotenv()
storage.init_db()

app = FastAPI(title="log-analyzer API", version="0.2.0")

# 開発用フロントエンドの origin を許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# このファイルは apps/agents/src/log_analyzer/api.py、リポジトリルートは 4 階層上
_REPO_ROOT = Path(__file__).resolve().parents[4]
_LOGS_DIR = _REPO_ROOT / "samples" / "logs"

# 比較ビュー（Phase 2 W6）で 4 構成同時実行することを想定し max_workers=4。
# 各 runner 内部でもモデル API を並列呼び出しするので、ピーク時の同時リクエスト数は 10+ になる
# （Anthropic / OpenAI のレート制限内に収まる前提）。
_executor = ThreadPoolExecutor(max_workers=4)


class ConfigEntry(BaseModel):
    id: str  # "config1" / "user:<id>"
    label: str
    type: str  # "builtin" / "user"
    base_config: str  # "config1" .. "config4"


class ConfigsResponse(BaseModel):
    configs: list[ConfigEntry]


class LogEntry(BaseModel):
    name: str
    bytes: int
    lines: int


class LogsResponse(BaseModel):
    logs: list[LogEntry]


class RunRequest(BaseModel):
    log_name: str
    config: str  # "config1" .. "config4" / "user:<id>"
    # ad-hoc overrides（builtin config 選択時に編集中の slot を即時試す経路）。
    # config が "user:<id>" の場合は無視され、保存済みの overrides が使われる。
    overrides: dict[str, str] | None = None
    model_overrides: dict[str, str] | None = None


class SlotInfo(BaseModel):
    slot_id: str
    label: str
    default_prompt: str
    default_model: str
    allowed_models: list[str]  # 空配列ならモデル上書き不可


class PromptSlotsResponse(BaseModel):
    base_config: str
    slots: list[SlotInfo]


class SavedConfigDTO(BaseModel):
    id: int
    name: str
    base_config: str
    overrides: dict[str, str]
    model_overrides: dict[str, str] = {}
    created_at: str
    updated_at: str


class SavedConfigsResponse(BaseModel):
    configs: list[SavedConfigDTO]


class CreateSavedConfigRequest(BaseModel):
    name: str
    base_config: str
    overrides: dict[str, str]
    model_overrides: dict[str, str] = {}


class UpdateSavedConfigRequest(BaseModel):
    overrides: dict[str, str]
    model_overrides: dict[str, str] = {}


@app.get("/api/configs", response_model=ConfigsResponse)
def list_configs() -> ConfigsResponse:
    builtins: list[ConfigEntry] = [
        ConfigEntry(id=cid, label=cid, type="builtin", base_config=cid)
        for cid in CONFIG_RUNNERS.keys()
    ]
    user_configs: list[ConfigEntry] = []
    for sc in storage.list_saved_configs():
        user_configs.append(
            ConfigEntry(
                id=f"user:{sc['id']}",
                label=f"{sc['name']}（{sc['base_config']} ベース）",
                type="user",
                base_config=sc["base_config"],
            )
        )
    return ConfigsResponse(configs=builtins + user_configs)


@app.get("/api/logs", response_model=LogsResponse)
def list_logs() -> LogsResponse:
    if not _LOGS_DIR.exists():
        raise HTTPException(status_code=500, detail=f"logs directory not found: {_LOGS_DIR}")
    entries: list[LogEntry] = []
    for path in sorted(_LOGS_DIR.glob("*.log")):
        text = path.read_text(encoding="utf-8", errors="replace")
        entries.append(
            LogEntry(
                name=path.name,
                bytes=path.stat().st_size,
                lines=len(text.splitlines()),
            )
        )
    return LogsResponse(logs=entries)


@app.get("/api/prompt-slots/{base_config}", response_model=PromptSlotsResponse)
def get_prompt_slots(base_config: str) -> PromptSlotsResponse:
    if base_config not in prompt_slots.VALID_BASE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"unknown base_config: {base_config}")
    slots = prompt_slots.get_slots(base_config)
    return PromptSlotsResponse(
        base_config=base_config,
        slots=[SlotInfo(**s) for s in slots],
    )


@app.get("/api/configs/saved", response_model=SavedConfigsResponse)
def list_saved_configs_endpoint() -> SavedConfigsResponse:
    return SavedConfigsResponse(
        configs=[SavedConfigDTO(**sc) for sc in storage.list_saved_configs()]
    )


def _validate_overrides(base_config: str, prompt_overrides: dict[str, str], model_overrides: dict[str, str]) -> None:
    slots = prompt_slots.get_slots(base_config)
    valid_slot_ids = {s["slot_id"] for s in slots}
    invalid_p = [sid for sid in prompt_overrides if sid not in valid_slot_ids]
    if invalid_p:
        raise HTTPException(status_code=400, detail=f"unknown slot_id(s) for {base_config}: {invalid_p}")
    invalid_m = [sid for sid in model_overrides if sid not in valid_slot_ids]
    if invalid_m:
        raise HTTPException(status_code=400, detail=f"unknown model slot_id(s) for {base_config}: {invalid_m}")
    # 各 slot の allowed_models をチェック（empty なら model 上書き不可）
    by_id = {s["slot_id"]: s for s in slots}
    for sid, model_name in model_overrides.items():
        allowed = by_id[sid]["allowed_models"]
        if not allowed:
            raise HTTPException(status_code=400, detail=f"slot '{sid}' は model 上書き不可")
        if model_name not in allowed:
            raise HTTPException(status_code=400, detail=f"slot '{sid}' に許可されないモデル: {model_name}")


@app.post("/api/configs/saved", response_model=SavedConfigDTO)
def create_saved_config_endpoint(req: CreateSavedConfigRequest) -> SavedConfigDTO:
    if req.base_config not in prompt_slots.VALID_BASE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"unknown base_config: {req.base_config}")
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name は必須")
    _validate_overrides(req.base_config, req.overrides, req.model_overrides)
    try:
        saved = storage.create_saved_config(
            req.name, req.base_config, req.overrides, req.model_overrides
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"保存失敗: {e}")
    return SavedConfigDTO(**saved)


@app.put("/api/configs/saved/{config_id}", response_model=SavedConfigDTO)
def update_saved_config_endpoint(config_id: int, req: UpdateSavedConfigRequest) -> SavedConfigDTO:
    existing = storage.get_saved_config(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"config id={config_id} not found")
    _validate_overrides(existing["base_config"], req.overrides, req.model_overrides)
    saved = storage.update_saved_config(config_id, req.overrides, req.model_overrides)
    if saved is None:
        raise HTTPException(status_code=404, detail=f"config id={config_id} not found")
    return SavedConfigDTO(**saved)


@app.delete("/api/configs/saved/{config_id}")
def delete_saved_config_endpoint(config_id: int) -> dict:
    deleted = storage.delete_saved_config(config_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"config id={config_id} not found")
    return {"deleted": config_id}


def _resolve_run_target(
    config: str,
    ad_hoc_prompt_overrides: dict[str, str] | None,
    ad_hoc_model_overrides: dict[str, str] | None,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """`config` 文字列を `(base_config, prompt_overrides, model_overrides)` に解決する。

    builtin: base_config はそのまま、overrides は ad-hoc 値
    user:<id>: 保存済みから読み出す（ad-hoc は無視）
    """
    if config in CONFIG_RUNNERS:
        return config, ad_hoc_prompt_overrides or {}, ad_hoc_model_overrides or {}
    if config.startswith("user:"):
        try:
            saved_id = int(config.split(":", 1)[1])
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid user config id: {config}")
        saved = storage.get_saved_config(saved_id)
        if saved is None:
            raise HTTPException(status_code=404, detail=f"saved config id={saved_id} not found")
        return (
            saved["base_config"],
            dict(saved["overrides"]),
            dict(saved.get("model_overrides", {})),
        )
    raise HTTPException(status_code=400, detail=f"unknown config: {config}")


@app.post("/api/runs", response_model=AnalysisResult)
async def run_config(req: RunRequest) -> AnalysisResult:
    log_path = _LOGS_DIR / req.log_name
    if not log_path.exists() or log_path.suffix != ".log":
        raise HTTPException(status_code=404, detail=f"log not found: {req.log_name}")

    base_config, p_overrides, m_overrides = _resolve_run_target(
        req.config, req.overrides, req.model_overrides
    )
    if p_overrides or m_overrides:
        # ad-hoc / 保存済みのいずれでも slot_id とモデル名を検証
        _validate_overrides(base_config, p_overrides, m_overrides)

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    runner = CONFIG_RUNNERS[base_config]

    loop = asyncio.get_running_loop()
    # runner は sync で 30〜60 秒かかる。event loop を塞がないよう thread pool にオフロード
    result = await loop.run_in_executor(
        _executor,
        lambda: runner(
            log_text,
            str(log_path),
            prompt_overrides=p_overrides,
            model_overrides=m_overrides,
        ),
    )
    return result
