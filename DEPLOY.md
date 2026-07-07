# OpenAgentOS 部署（Docker Compose 全栈）

一套 compose 拉起全部依赖，Linux 与 Windows Docker Desktop 通用。

## 部署了什么

| 服务 | 作用 |
|---|---|
| `postgres`（pgvector） | Aegra 持久化 / 检查点 / 语义 store |
| `redis` | Aegra 任务队列 / SSE pub-sub / 崩溃恢复 |
| `opensandbox-server` | 经宿主 docker.sock 拉起/管理沙箱容器（execd 镜像 `opensandbox/execd`） |
| `openagentos`（app） | `aegra serve`，托管 `agentos` 图 |

一块**共享磁盘**（宿主 `AGENTOS_WORKSPACE_HOST`）同时挂给 app 与每个沙箱：app 读 `.mcp.json`、
回传下载；沙箱按 `subPath` bind `/workspace`（线程私有）与 `/workspace/skills`（助手级）。

## 目录约定

- `/data/git/openagentos` — 代码（`git clone`，只读源）。
- `/data/openagentos` — 编译 / 部署目录（compose 在此运行）。代码从 git 目录同步过来
  （`rsync` / CI / 手动皆可），`.env` 独立维护。
- `/data/openagentos/workspace` — 共享工作区宿主目录（`AGENTOS_WORKSPACE_HOST`）。

> 也可直接在 `/data/git/openagentos` 里跑 compose，省去同步；两段路径是「源码 vs 部署」的约定，按需取舍。

## 前置

- Docker Engine + Compose v2（Linux）或 Docker Desktop（Windows / macOS，建议 WSL2 后端）。
- 镜像 `opensandbox/server:latest`、`opensandbox/execd:v1.0.19` 可拉取（内网改镜像源）。
- 一个 OpenAI 兼容网关（或每 assistant 在 config 里自带连接）。

## 步骤（Linux / WSL2 / macOS）

```bash
# 1) 拉代码
git clone <repo-url> /data/git/openagentos

# 2) 部署目录 + 共享工作区 + .env（唯一必改 POSTGRES_PASSWORD）
mkdir -p /data/openagentos /data/openagentos/workspace
rsync -a --exclude='.env' --exclude='.git/' --exclude='.venv/' \
  /data/git/openagentos/ /data/openagentos/
cp /data/git/openagentos/.env.example /data/openagentos/.env
$EDITOR /data/openagentos/.env        # 设 POSTGRES_PASSWORD；OPENAI_* 作全局兜底（可选）

# 3) 起全栈（构建 + 后台）
cd /data/openagentos && docker compose up -d --build
#   停止： docker compose down          （连数据卷一起删： docker compose down -v）
#   日志： docker compose logs -f --tail 50 openagentos
```

## Windows Docker Desktop

- **推荐**：在 WSL2 发行版里按上面的 Linux 步骤跑（`/data/...` 即 WSL2 路径，bind 最快）。
- **原生**：把代码与 `.env` 放到某目录（如 `C:\data\openagentos`），并把 `.env` 的
  `AGENTOS_WORKSPACE_HOST` 改成 Docker Desktop 可 bind 的 Windows 路径，然后：
  ```powershell
  cd C:\data\openagentos
  docker compose up -d --build
  ```

## 端口（均可在 .env 改）

- app（Aegra）：`http://localhost:2026` — `/docs`、`/health`
- postgres `15432` · redis `16379` · opensandbox `8080`

## 每 assistant 用法

创建 assistant 时把 `model/prompt/api_key/base_url` 放进 `config.configurable`；MCP 与 skills
放共享磁盘 `workspace/.deepagent/<assistant_id>/`（`.mcp.json` 与 `skills/`）。详见 [README](README.md)。

## 注意

- **`sandbox.toml`**：默认 `host_ip = host.docker.internal`、
  `allowed_host_paths = ['/data/openagentos/workspace']`。换宿主 IP（Linux 被防火墙挡时）或
  换工作区路径时，手动改这两处，并 `docker compose up -d --force-recreate opensandbox-server` 使其生效。
- 沙箱由宿主 Docker 经 `docker.sock` 拉起，与 compose 服务同网络
  （`sandbox.toml` 的 `network_mode = openagentos_default`，即 `<部署目录名>_default`）。
- 迁移：`RUN_MIGRATIONS_ON_STARTUP=true` 启动自动迁移；多实例部署设 `false` 并带外
  `aegra db upgrade`。
- 共享磁盘是唯一持久真源；沙箱可弃（TTL 到期销毁，下次操作重挂同一 `subPath` 重建）。
- 单机自托管假设（共享磁盘走宿主目录）；多节点请改用 K8s + PVC（设 `AGENTOS_WORKSPACE_CLAIM`）。
