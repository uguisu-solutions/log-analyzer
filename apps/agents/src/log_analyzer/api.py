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
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from log_analyzer import pipeline_runner, prompt_slots, storage
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

# UI 表示用の説明的ラベル。コード ID（config1..config5）はトレース名・DB・JSON で固定。
# モデル名は slot 別に上書き可能なのでラベルには含めない（パイプライン構造のみ表現）。
BUILTIN_CONFIG_LABELS: dict[str, str] = {
    "config1": "config1 — ベースライン（単一 LLM）",
    "config2": "config2 — フィルタ + 圧縮（前処理 → 分析の 2 段）",
    "config3": "config3 — マルチモデル並列（3 モデル並列 → 統合）",
    "config4": "config4 — オーケストレータ駆動（LangGraph orchestrator が監視を再評価しながらラリー）",
    "config5": "config5 — ユーザー定義パイプライン（DAG 自由設計）",
}

# 選択 UI から除外する builtin。config5 は pipeline_def 必須なので、
# 直接 builtin として選んでも実行不可。ユーザーは構成設計タブで pipeline を作成し、
# user:N として保存してから利用する。
HIDDEN_BUILTIN_CONFIGS: set[str] = {"config5"}


# config1〜4 の固定グラフ構造（React Flow 描画用）。
# 各ノードは type / slot_id / fixed_model を持つ:
#   - type=input: 入力ノード（編集不可）
#   - type=slot: 通常の slot ノード（slot_id でプロンプト/モデル編集可）
#   - type=slot_instance: 複数モデル並列で同じ slot を共有する場合（モデルは固定）
BUILTIN_STRUCTURES: dict[str, dict] = {
    "config1": {
        "nodes": [
            {"id": "input", "type": "input", "label": "入力ログ"},
            {"id": "analyze", "type": "slot", "slot_id": "analyze", "label": "分析（最終出力）"},
        ],
        "edges": [
            {"source": "input", "target": "analyze"},
        ],
    },
    "config2": {
        "nodes": [
            {"id": "input", "type": "input", "label": "入力ログ"},
            {"id": "filter_stage", "type": "static", "label": "ルールフィルタ\n(deterministic)"},
            {"id": "triage", "type": "slot", "slot_id": "triage", "label": "Triage 圧縮"},
            {"id": "analyze", "type": "slot", "slot_id": "analyze", "label": "分析（最終出力）"},
        ],
        "edges": [
            {"source": "input", "target": "filter_stage"},
            {"source": "filter_stage", "target": "triage"},
            {"source": "triage", "target": "analyze"},
        ],
    },
    "config3": {
        "nodes": [
            {"id": "input", "type": "input", "label": "入力ログ"},
            {
                "id": "sonnet",
                "type": "slot_instance",
                "slot_id": "analyze",
                "fixed_model": "claude-sonnet-4-5",
                "label": "Sonnet\n(slot: analyze)",
            },
            {
                "id": "haiku",
                "type": "slot_instance",
                "slot_id": "analyze",
                "fixed_model": "claude-haiku-4-5",
                "label": "Haiku\n(slot: analyze)",
            },
            {
                "id": "openai",
                "type": "slot_instance",
                "slot_id": "analyze",
                "fixed_model": "gpt-4o-mini",
                "label": "GPT-4o-mini\n(slot: analyze)",
            },
            {"id": "integrate", "type": "slot", "slot_id": "integrate", "label": "統合（最終出力）"},
        ],
        "edges": [
            {"source": "input", "target": "sonnet"},
            {"source": "input", "target": "haiku"},
            {"source": "input", "target": "openai"},
            {"source": "sonnet", "target": "integrate"},
            {"source": "haiku", "target": "integrate"},
            {"source": "openai", "target": "integrate"},
        ],
    },
    "config4": {
        "nodes": [
            {"id": "input", "type": "input", "label": "入力ログ"},
            {"id": "orchestrator", "type": "slot", "slot_id": "orchestrator", "label": "オーケストレータ\n(再入・最大3ラウンド)"},
            {"id": "fw_monitor", "type": "slot", "slot_id": "fw_monitor", "label": "FW 監視"},
            {"id": "routing_monitor", "type": "slot", "slot_id": "routing_monitor", "label": "Routing 監視"},
            {"id": "app_monitor", "type": "slot", "slot_id": "app_monitor", "label": "App 監視"},
            {"id": "integrator", "type": "slot", "slot_id": "integrator", "label": "統合（最終出力）"},
        ],
        "edges": [
            {"source": "input", "target": "orchestrator"},
            {"source": "orchestrator", "target": "fw_monitor"},
            {"source": "orchestrator", "target": "routing_monitor"},
            {"source": "orchestrator", "target": "app_monitor"},
            {"source": "fw_monitor", "target": "orchestrator"},
            {"source": "routing_monitor", "target": "orchestrator"},
            {"source": "app_monitor", "target": "orchestrator"},
            {"source": "orchestrator", "target": "integrator"},
        ],
    },
}

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
    modified_at: str  # ISO8601 (UTC)


class LogsResponse(BaseModel):
    logs: list[LogEntry]


class LogContentResponse(BaseModel):
    name: str
    bytes: int
    total_lines: int
    preview_lines: int  # 実際に返した行数
    truncated: bool
    content: str  # 先頭 preview_lines 行をくっつけたテキスト


class LogUploadResponse(BaseModel):
    name: str
    bytes: int
    lines: int


# アップロード制限
_MAX_LOG_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
_PREVIEW_MAX_LINES = 200  # プレビューで返す最大行数
# パストラバーサル防止のためファイル名は英数字 / _ - . のみ許可
_VALID_LOG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.log$")


def _safe_log_path(name: str) -> Path:
    """``name`` を検証し、samples/logs/ 配下の安全な絶対パスを返す。

    パスセパレータや `..` を含む名前、`.log` 以外の拡張子は弾く。
    """
    if not _VALID_LOG_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "ファイル名は英数字 / _ / - / . のみ、拡張子は .log を必須とします: "
                f"{name!r}"
            ),
        )
    target = (_LOGS_DIR / name).resolve()
    # _LOGS_DIR の外を指していないことを念のため確認（_VALID_LOG_NAME_RE で
    # パスセパレータは弾いているが多重防御）
    if _LOGS_DIR.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail=f"invalid log path: {name}")
    return target


class RunRequest(BaseModel):
    log_name: str
    config: str  # "config1" .. "config5" / "user:<id>"
    # ad-hoc overrides（builtin config 選択時に編集中の slot を即時試す経路）。
    # config が "user:<id>" の場合は無視され、保存済みの値が使われる。
    overrides: dict[str, str] | None = None
    model_overrides: dict[str, str] | None = None
    # config5（user_pipeline）でのみ使用。ad-hoc プレビュー実行時にここで pipeline_def を渡す。
    pipeline: dict | None = None
    # config4（rally）専用ランタイムパラメータ。base_config が config4 のときのみ有効。
    rally_max_rounds: int | None = None
    rally_force_min_rounds: int | None = None


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
    pipeline: dict | None = None
    created_at: str
    updated_at: str


class SavedConfigsResponse(BaseModel):
    configs: list[SavedConfigDTO]


class CreateSavedConfigRequest(BaseModel):
    name: str
    base_config: str
    overrides: dict[str, str] = {}
    model_overrides: dict[str, str] = {}
    pipeline: dict | None = None


class UpdateSavedConfigRequest(BaseModel):
    overrides: dict[str, str] = {}
    model_overrides: dict[str, str] = {}
    pipeline: dict | None = None


class NodeTypeDef(BaseModel):
    type: str
    label: str
    description: str
    fixed: bool
    editable_fields: list[str]
    default_prompt: str | None = None
    default_model: str | None = None
    default_input_template: str | None = None


class NodeTypesResponse(BaseModel):
    node_types: list[NodeTypeDef]
    allowed_models: list[str]


class PipelineDefaultResponse(BaseModel):
    pipeline: dict


@app.get("/api/configs", response_model=ConfigsResponse)
def list_configs() -> ConfigsResponse:
    builtins: list[ConfigEntry] = [
        ConfigEntry(
            id=cid,
            label=BUILTIN_CONFIG_LABELS.get(cid, cid),
            type="builtin",
            base_config=cid,
        )
        for cid in CONFIG_RUNNERS.keys()
        if cid not in HIDDEN_BUILTIN_CONFIGS
    ]
    user_configs: list[ConfigEntry] = []
    for sc in storage.list_saved_configs():
        base = sc["base_config"]
        # config5（pipeline）派生は構造そのものをユーザーが設計しているのでベース表記不要、
        # config1〜4 派生は slot 上書きなのでベース構成が分かる方が選択時に判断しやすい
        if base == "config5":
            label = sc["name"]
        else:
            base_label = BUILTIN_CONFIG_LABELS.get(base, base)
            base_short = base_label.split("—", 1)[-1].split("（", 1)[0].split("(", 1)[0].strip()
            label = f"{sc['name']}（{base}: {base_short}）"
        user_configs.append(
            ConfigEntry(
                id=f"user:{sc['id']}",
                label=label,
                type="user",
                base_config=base,
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
        stat = path.stat()
        entries.append(
            LogEntry(
                name=path.name,
                bytes=stat.st_size,
                lines=len(text.splitlines()),
                modified_at=datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            )
        )
    return LogsResponse(logs=entries)


@app.post("/api/logs", response_model=LogUploadResponse)
async def upload_log(file: UploadFile = File(...)) -> LogUploadResponse:
    """`.log` ファイルを samples/logs/ にアップロードする。

    ルール:
    - 拡張子は .log のみ
    - 同名既存があれば 409 で拒否（事故防止）
    - 10 MB を超えるファイルは 413 で拒否
    """
    if file.filename is None:
        raise HTTPException(status_code=400, detail="ファイル名が空です")
    target = _safe_log_path(file.filename)

    if target.exists():
        raise HTTPException(
            status_code=409,
            detail=f"同名のログが既に存在します: {file.filename}",
        )

    # サイズ制限はチャンクで読みつつ判定（メモリで丸ごと持たない）
    written = 0
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    try:
        with tmp_path.open("wb") as fh:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_LOG_SIZE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"ファイルサイズが上限を超えています "
                            f"({written} > {_MAX_LOG_SIZE_BYTES} bytes)"
                        ),
                    )
                fh.write(chunk)
        tmp_path.rename(target)
    except HTTPException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink()
        raise HTTPException(status_code=500, detail=f"アップロード失敗: {e}")

    text = target.read_text(encoding="utf-8", errors="replace")
    return LogUploadResponse(
        name=target.name,
        bytes=written,
        lines=len(text.splitlines()),
    )


@app.get("/api/logs/{name}/content", response_model=LogContentResponse)
def get_log_content(name: str) -> LogContentResponse:
    """ログ先頭 N 行（既定 200 行）をプレビュー用に返す。"""
    target = _safe_log_path(name)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"ログが見つかりません: {name}")
    text = target.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    preview = all_lines[:_PREVIEW_MAX_LINES]
    return LogContentResponse(
        name=name,
        bytes=target.stat().st_size,
        total_lines=len(all_lines),
        preview_lines=len(preview),
        truncated=len(all_lines) > len(preview),
        content="\n".join(preview),
    )


@app.delete("/api/logs/{name}")
def delete_log(name: str) -> dict:
    """ログファイルを削除。"""
    target = _safe_log_path(name)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"ログが見つかりません: {name}")
    try:
        target.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"削除失敗: {e}")
    return {"deleted": name}


class BuiltinStructureResponse(BaseModel):
    base_config: str
    nodes: list[dict]
    edges: list[dict]


@app.get("/api/configs/{base_config}/structure", response_model=BuiltinStructureResponse)
def get_builtin_structure(base_config: str) -> BuiltinStructureResponse:
    """builtin config1〜4 の固定グラフ構造を返す（React Flow 描画用）。"""
    if base_config not in BUILTIN_STRUCTURES:
        raise HTTPException(
            status_code=400,
            detail=f"no fixed structure for base_config={base_config}",
        )
    s = BUILTIN_STRUCTURES[base_config]
    return BuiltinStructureResponse(
        base_config=base_config,
        nodes=s["nodes"],
        edges=s["edges"],
    )


@app.get("/api/prompt-slots/{base_config}", response_model=PromptSlotsResponse)
def get_prompt_slots(base_config: str) -> PromptSlotsResponse:
    if base_config not in prompt_slots.VALID_BASE_CONFIGS:
        raise HTTPException(status_code=400, detail=f"unknown base_config: {base_config}")
    slots = prompt_slots.get_slots(base_config)
    return PromptSlotsResponse(
        base_config=base_config,
        slots=[SlotInfo(**s) for s in slots],
    )


@app.get("/api/node-types", response_model=NodeTypesResponse)
def get_node_types() -> NodeTypesResponse:
    """構成5（user_pipeline）の UI で使えるノードタイプ定義を返す。"""
    return NodeTypesResponse(
        node_types=[NodeTypeDef(**nt) for nt in pipeline_runner.NODE_TYPE_DEFS],
        allowed_models=pipeline_runner.ALLOWED_MODELS,
    )


@app.get("/api/pipelines/default", response_model=PipelineDefaultResponse)
def get_default_pipeline() -> PipelineDefaultResponse:
    """新規 pipeline 作成時の出発点（input → output の最小構成）を返す。"""
    return PipelineDefaultResponse(
        pipeline={
            "nodes": [
                {"id": "input", "type": "input"},
                {
                    "id": "output",
                    "type": "output",
                    "prompt": pipeline_runner.DEFAULT_OUTPUT_PROMPT,
                    "model": "claude-sonnet-4-5",
                    "input_template": pipeline_runner.DEFAULT_INPUT_TEMPLATE_OUTPUT,
                },
            ],
            "edges": [{"source": "input", "target": "output"}],
        }
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
    if req.base_config == "config5":
        if not req.pipeline:
            raise HTTPException(status_code=400, detail="config5 は pipeline が必須")
        try:
            pipeline_runner.validate_pipeline(req.pipeline)
        except pipeline_runner.PipelineValidationError as e:
            raise HTTPException(status_code=400, detail=f"pipeline 検証失敗: {e}")
    else:
        _validate_overrides(req.base_config, req.overrides, req.model_overrides)
    try:
        saved = storage.create_saved_config(
            req.name, req.base_config, req.overrides, req.model_overrides, req.pipeline
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"保存失敗: {e}")
    return SavedConfigDTO(**saved)


@app.put("/api/configs/saved/{config_id}", response_model=SavedConfigDTO)
def update_saved_config_endpoint(config_id: int, req: UpdateSavedConfigRequest) -> SavedConfigDTO:
    existing = storage.get_saved_config(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"config id={config_id} not found")
    if existing["base_config"] == "config5":
        if not req.pipeline:
            raise HTTPException(status_code=400, detail="config5 は pipeline が必須")
        try:
            pipeline_runner.validate_pipeline(req.pipeline)
        except pipeline_runner.PipelineValidationError as e:
            raise HTTPException(status_code=400, detail=f"pipeline 検証失敗: {e}")
    else:
        _validate_overrides(existing["base_config"], req.overrides, req.model_overrides)
    saved = storage.update_saved_config(
        config_id, req.overrides, req.model_overrides, req.pipeline
    )
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
    ad_hoc_pipeline: dict | None,
) -> tuple[str, dict[str, str], dict[str, str], dict | None]:
    """`config` 文字列を `(base_config, prompt_overrides, model_overrides, pipeline)` に解決。

    builtin: base_config はそのまま、上書き類は ad-hoc 値
    user:<id>: 保存済みから読み出す（ad-hoc は無視）
    """
    if config in CONFIG_RUNNERS:
        return (
            config,
            ad_hoc_prompt_overrides or {},
            ad_hoc_model_overrides or {},
            ad_hoc_pipeline,
        )
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
            saved.get("pipeline"),
        )
    raise HTTPException(status_code=400, detail=f"unknown config: {config}")


@app.post("/api/runs", response_model=AnalysisResult)
async def run_config(req: RunRequest) -> AnalysisResult:
    log_path = _LOGS_DIR / req.log_name
    if not log_path.exists() or log_path.suffix != ".log":
        raise HTTPException(status_code=404, detail=f"log not found: {req.log_name}")

    base_config, p_overrides, m_overrides, pipeline = _resolve_run_target(
        req.config, req.overrides, req.model_overrides, req.pipeline
    )

    if base_config == "config5":
        if not pipeline:
            raise HTTPException(status_code=400, detail="config5 は pipeline が必須")
        try:
            pipeline_runner.validate_pipeline(pipeline)
        except pipeline_runner.PipelineValidationError as e:
            raise HTTPException(status_code=400, detail=f"pipeline 検証失敗: {e}")
    else:
        if p_overrides or m_overrides:
            _validate_overrides(base_config, p_overrides, m_overrides)

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    runner = CONFIG_RUNNERS[base_config]

    loop = asyncio.get_running_loop()
    if base_config == "config5":
        result = await loop.run_in_executor(
            _executor,
            lambda: runner(
                log_text, str(log_path), pipeline_def=pipeline,
            ),
        )
    elif base_config == "config4":
        result = await loop.run_in_executor(
            _executor,
            lambda: runner(
                log_text,
                str(log_path),
                prompt_overrides=p_overrides,
                model_overrides=m_overrides,
                rally_max_rounds=req.rally_max_rounds,
                rally_force_min_rounds=req.rally_force_min_rounds,
            ),
        )
    else:
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
