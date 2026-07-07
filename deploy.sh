#!/usr/bin/env bash
# OpenAgentOS 部署（Linux / WSL2 / macOS / Git-Bash）：
#   同步 /data/git/openagentos -> /data/openagentos（排除 .env）→ 构建 → compose up → 健康检查。
# git pull 与 .env 由人工维护，本脚本不碰。用法：./deploy.sh [已有tag则跳过构建]
set -euo pipefail

GIT_DIR="${GIT_DIR:-/data/git/openagentos}"    # 代码目录（只读源）
DEPLOY_DIR="${DEPLOY_DIR:-/data/openagentos}"  # 构建 + 部署目录（自动同步）
ENV_FILE="$DEPLOY_DIR/.env"                    # 手动维护，不碰
HOST_IP="${HOST_IP:-host.docker.internal}"     # sandbox.toml host_ip；Linux 被防火墙挡时设宿主真实 IP

say() { printf '\033[1;34m[STEP]\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found in PATH"
command -v rsync  >/dev/null || die "rsync not found in PATH"
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE=(docker-compose)
else die "neither 'docker compose' nor 'docker-compose' available"; fi

[[ -f "$GIT_DIR/Dockerfile" ]] || die "no Dockerfile under $GIT_DIR (git clone first?)"
mkdir -p "$DEPLOY_DIR"
[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE -> cp $GIT_DIR/.env.example $ENV_FILE and edit"
grep -qE '^POSTGRES_PASSWORD=.+' "$ENV_FILE" || die "$ENV_FILE: POSTGRES_PASSWORD empty"

# 共享工作区宿主目录（app 与所有沙箱共用）：按 .env 的 AGENTOS_WORKSPACE_HOST 创建。
WS_HOST=$(sed -n 's/^AGENTOS_WORKSPACE_HOST=//p' "$ENV_FILE" | head -1)
WS_HOST="${WS_HOST:-/data/openagentos/workspace}"
mkdir -p "$WS_HOST" || die "cannot mkdir workspace: $WS_HOST"
ok "workspace dir ready: $WS_HOST"

say "sync $GIT_DIR -> $DEPLOY_DIR"
rsync -a --delete \
  --exclude='.env' --exclude='.env.*' \
  --exclude='.git/' --exclude='.venv/' \
  --exclude='__pycache__/' --exclude='*.py[cod]' \
  --exclude='.pytest_cache/' --exclude='.ruff_cache/' --exclude='.mypy_cache/' \
  "$GIT_DIR"/ "$DEPLOY_DIR"/
ok "code synced (.env preserved)"

# 注入宿主 IP + 工作区路径到 sandbox.toml（换机器 / 换路径即生效）。
sed -i -E "s|^host_ip = .*|host_ip = \"$HOST_IP\"|" "$DEPLOY_DIR/sandbox.toml"
sed -i -E "s|^allowed_host_paths = .*|allowed_host_paths = ['$WS_HOST']|" "$DEPLOY_DIR/sandbox.toml"
ok "sandbox.toml host_ip=$HOST_IP workspace=$WS_HOST"

if [[ $# -ge 1 ]]; then TAG="$1"; BUILD=(); say "redeploy tag $TAG (skip build)"
else TAG=$(date +%Y%m%d-%H%M); BUILD=(--build); say "build + deploy tag $TAG"; fi
(cd "$DEPLOY_DIR" && TAG="$TAG" "${COMPOSE[@]}" up -d "${BUILD[@]}" --remove-orphans)
# sandbox.toml 改动仅在服务重启后生效（compose 不因 bind 文件变化重建），显式重建。
(cd "$DEPLOY_DIR" && TAG="$TAG" "${COMPOSE[@]}" up -d --force-recreate opensandbox-server)

APP=$(sed -n 's/^PROJECT_NAME=//p' "$ENV_FILE" | head -1); APP="${APP:-openagentos}"
say "waiting for $APP healthy ..."
status=missing
for _ in $(seq 1 60); do
  status=$(docker inspect "$APP" --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)
  case "$status" in
    healthy)   ok "$APP healthy"; break ;;
    unhealthy) docker logs --tail 40 "$APP"; die "$APP unhealthy" ;;
    missing)   die "$APP not found - up failed?" ;;
    *)         printf '.'; sleep 2 ;;
  esac
done
[[ "$status" == healthy ]] || { echo; die "$APP not healthy within ~120s (docker logs $APP)"; }
echo; ok "DEPLOY OK -- tag $TAG"; say "tail logs: docker logs -f --tail 50 $APP"
