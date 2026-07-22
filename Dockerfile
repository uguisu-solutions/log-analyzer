# syntax=docker/dockerfile:1
#
# log-analyzer バックエンド（FastAPI + uvicorn）を Cloud Run で起動するイメージ。
#
# 方針（hosting-refactor-policy.md #6 step1）:
#   まず「今の SQLite/FS のまま」動くコンテナを作る。DB/ストレージの本番化は後段で
#   env（DATABASE_URL / *_STORE）を差し替えて切替える。
#
# 重要: アプリは `Path(__file__).resolve().parents[4]` でリポジトリルートを求め、
#   そこから `samples/logs`・`samples/source` を参照する。そのため **editable install**
#   でソースを /app 配下に残し、レイアウト（/app/apps/agents/src/log_analyzer/...）を保つ。

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# --- 依存 + アプリ（editable） ---
# editable install には src が必要なため、pyproject と src を先にコピーする。
COPY apps/agents/pyproject.toml /app/apps/agents/pyproject.toml
COPY apps/agents/src /app/apps/agents/src
# manylinux ホイールで解決される想定（psycopg[binary] / tree-sitter-language-pack 等）。
# 万一ソースビルドが要る環境では build-essential を足すこと。
RUN pip install -e /app/apps/agents

# --- BigQuery MCP Toolbox 設定（ハードニング版 yaml を同梱） ---
# prebuilt 運用時は未使用だが、--config 方式へ切替えられるよう同梱しておく。
COPY apps/agents/config /app/apps/agents/config

# --- ランタイムが参照する samples レイアウト ---
# 検証用サンプルを同梱（source アップロード実体と data は .dockerignore で除外）。
COPY samples /app/samples
# アップロード先/DB ディレクトリを用意（未存在だと GET /api/logs が 500 になる）。
RUN mkdir -p /app/samples/logs /app/samples/source /app/apps/agents/data

# --- BigQuery MCP Toolbox（任意同梱） ---
# 既定は同梱しない（step1 のビルドを確実にするため）。BQ 取得を使うなら
#   --build-arg INSTALL_TOOLBOX=true --build-arg TOOLBOX_VERSION=<ver>
# を渡す。公式配布: https://storage.googleapis.com/genai-toolbox/v<ver>/linux/amd64/toolbox
ARG INSTALL_TOOLBOX=false
ARG TOOLBOX_VERSION=0.5.0
RUN if [ "$INSTALL_TOOLBOX" = "true" ]; then \
      set -eux; \
      apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; \
      curl -fsSL -o /usr/local/bin/toolbox \
        "https://storage.googleapis.com/genai-toolbox/v${TOOLBOX_VERSION}/linux/amd64/toolbox"; \
      chmod +x /usr/local/bin/toolbox; \
      apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*; \
    fi

# Cloud Run は $PORT を注入する。0.0.0.0 待受・単一プロセスで起動。
# （SSE 維持のため Cloud Run 側で min-instances>=1 / CPU always-allocated / timeout=3600 を設定）
EXPOSE 8080
# JSON 形式で sh -c を包む: ${PORT} 展開と exec（uvicorn を PID 1 にして SIGTERM を
# 直接受け、graceful shutdown を効かせる）を両立しつつ CMD の警告も避ける。
CMD ["sh", "-c", "exec uvicorn log_analyzer.api:app --host 0.0.0.0 --port ${PORT}"]
