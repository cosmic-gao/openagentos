# OpenAgentOS app 镜像：多阶段构建。builder 用 uv 装依赖（含 git 依赖
# deepagents-opensandbox，故装 git）；final 只带 venv + 运行所需源码，跑 `aegra serve`。
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app

FROM base AS builder

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    UV_INDEX_URL=${PIP_INDEX_URL}

# git：安装 deepagents-opensandbox（git 依赖）所需。
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.11.24

# package = false：只装依赖，本项目 agentos 经 aegra.json `dependencies: ["."]` 上 sys.path。
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --compile-bytecode

FROM base AS final
COPY --from=builder /app/.venv /app/.venv
COPY aegra.json ./
COPY agentos/ ./agentos/

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 2026
USER app
CMD ["aegra", "serve"]
