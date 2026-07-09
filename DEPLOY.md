# OpenAgentOS 部署（Docker Compose 全栈）

一套 compose 拉起全部依赖，Linux 与 Windows Docker Desktop 通用。

## 部署了什么

| 服务 | 作用 |
|---|---|
| `postgres`（pgvector） | Aegra 持久化 / 检查点 / 语义 store |
| `redis` | Aegra 任务队列 / SSE pub-sub / 崩溃恢复 |
| `sandbox-config` | 一次性 init：把 `AGENTOS_WORKSPACE_HOST` 注入 `sandbox.toml` 的 `allowed_host_paths` |
| `opensandbox-server` | 经宿主 docker.sock 拉起/管理沙箱容器（execd 镜像 `opensandbox/execd`） |
| `openagentos`（app） | `aegra serve`，托管 `agentos` 图 |
| `langfuse-*`（profile `langfuse`，opt-in） | 可观测性:每次 LLM 调用的 token/耗时/成本进 trace（见 [可观测性](#可观测性otel--langfuseopt-in)） |

一块**外挂磁盘**上的**共享工作区**（宿主 `AGENTOS_WORKSPACE_HOST`，唯一真源）同时挂给 app 与每个
沙箱：app 读 `.mcp.json`、回传下载；沙箱按 `subPath` bind `/workspace`（线程私有）与
`/workspace/skills`（助手级）。该路径既是 bind 源，又被注入 `sandbox.toml`（见下方「注意」）。

## 目录约定

- `/data/git/openagentos` — 代码（`git clone`，只读源）。
- `/data/openagentos` — 编译 / 部署目录（compose 在此运行）。代码从 git 目录同步过来
  （`rsync` / CI / 手动皆可），`.env` 独立维护。
- `/data/openagentos/workspace` — 共享工作区宿主目录（`AGENTOS_WORKSPACE_HOST`），应在外挂磁盘挂载点下。

> 也可直接在 `/data/git/openagentos` 里跑 compose，省去同步；两段路径是「源码 vs 部署」的约定，按需取舍。

## 前置

- Docker Engine + Compose v2（Linux）或 Docker Desktop（Windows / macOS，建议 WSL2 后端）。
- 镜像 `opensandbox/server:latest`、`opensandbox/execd:v1.0.19` 可拉取（内网改镜像源）。
- 一个 OpenAI 兼容网关（或每 assistant 在 config 里自带连接）。

## 步骤（Linux / WSL2 / macOS）

```bash
# 1) 拉代码
git clone <repo-url> /data/git/openagentos

# 2) 部署目录 + 共享工作区（在外挂盘挂载点下）+ 未挂载哨兵 + .env（必改 POSTGRES_PASSWORD、REDIS_PASSWORD）
mkdir -p /data/openagentos /data/openagentos/workspace
touch /data/openagentos/workspace/.mounted   # 未挂载保护哨兵；须落在外挂盘上（盘没挂→文件不在→app 拒启）
rsync -a --exclude='.env' --exclude='.git/' --exclude='.venv/' \
  /data/git/openagentos/ /data/openagentos/
cp /data/git/openagentos/.env.example /data/openagentos/.env
$EDITOR /data/openagentos/.env        # 设 POSTGRES_PASSWORD、REDIS_PASSWORD；OPENAI_* 作全局兜底（可选）

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

## 可观测性(OTEL / Langfuse,opt-in)

Aegra 已内置 OpenTelemetry + OpenInference 自动埋点:开启后**每次 LLM 调用的 token 数(输入/输出/
总)、耗时、模型名**都会作为 span 上报,Langfuse 按 trace/session 聚合并算成本。默认**不启**——这是
一个 opt-in 的 compose profile,不占资源。

Langfuse v3 栈为 6 个专用服务(`langfuse-web` / `langfuse-worker` / `langfuse-postgres` /
`langfuse-clickhouse` / `langfuse-redis` / `langfuse-minio`),与 Aegra 主栈的 postgres/redis
完全隔离,额外约 **4-6GB 内存**(ClickHouse 占大头)。

### 启用

```bash
# 1) 生成密钥并填进 .env(见 .env.example 末尾「Langfuse 栈」块)
openssl rand -hex 32     # → LANGFUSE_ENCRYPTION_KEY(须 64 位十六进制)
openssl rand -base64 32  # → LANGFUSE_SALT
openssl rand -base64 32  # → LANGFUSE_NEXTAUTH_SECRET
#    另设 LANGFUSE_{POSTGRES,CLICKHOUSE,REDIS,MINIO_ROOT}_PASSWORD、LANGFUSE_INIT_USER_PASSWORD

# 2) 打开 app 侧上报开关(.env「可观测性」块):
#    OTEL_TARGETS=LANGFUSE
#    LANGFUSE_PUBLIC_KEY=pk-lf-...   ← app 与 Langfuse 首启初始化同源一对 key
#    LANGFUSE_SECRET_KEY=sk-lf-...

# 3) 起全栈 + Langfuse profile(app 会自动重连 langfuse-web:3000)
docker compose --profile langfuse up -d --build
```

- **UI**:`http://localhost:3000`(`LANGFUSE_WEB_PORT` 可改),用 `LANGFUSE_INIT_USER_EMAIL` /
  `LANGFUSE_INIT_USER_PASSWORD` 登录;项目与 API key 已由 `LANGFUSE_INIT_*` 首启自动建好,无需手动复制。
- **key 同源**:app 的 `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` 同时用作 Langfuse 初始化的项目 key,
  故开箱即通;上报端点为 `http://langfuse-web:3000/api/public/otel/v1/traces`(compose 内网,已自动配好)。
- **顺序无所谓**:app 不 `depends_on` Langfuse。profile 没起或 Langfuse 还在启动时,app 只是导出失败并
  记日志(非致命);Langfuse 就绪后自动恢复上报。
- **关闭**:`docker compose --profile langfuse down`(仅停 Langfuse,主栈不动);把 `OTEL_TARGETS` 置空
  即彻底关掉 app 端上报。数据卷 `langfuse_*` 保留,`down -v` 才连数据一起删。
- **外部 Langfuse**:不想自托管就跳过 profile,只在 `.env` 设 `OTEL_TARGETS=LANGFUSE` +
  `LANGFUSE_BASE_URL=https://cloud.langfuse.com`(或自有地址)+ 该项目的 key 即可。

> ⚠️ **端口 3000**:Langfuse UI 默认发布到宿主所有网卡。公网机器请用防火墙/反代限制,或把
> `LANGFUSE_WEB_PORT` 映射改为仅本机。其余 5 个 Langfuse 服务不发布端口(仅 compose 内网可达)。

## 注意

- **工作区路径单一真源**：只在 `.env` 配 `AGENTOS_WORKSPACE_HOST` 一处——compose 既拿它做
  app/沙箱的 bind 源，又经 `sandbox-config` 注入 `sandbox.toml` 的 `allowed_host_paths`，改后
  `docker compose up -d` 即重渲染生效。
- **`sandbox.toml` 的 `host_ip`**：默认 `host.docker.internal`；Linux 被防火墙挡时改宿主真实 IP，
  再 `docker compose up -d --force-recreate opensandbox-server`。
- **外挂盘写权限**：app 默认 `0:0`（root），与沙箱一致、bind 直接可写。收紧为非 root 时设
  `AGENTOS_UID/AGENTOS_GID` 为外挂盘属主并 `chown -R` 工作区。
- **未挂载保护**：`AGENTOS_WORKSPACE_SENTINEL`（默认 `.mounted`）不在工作区内则 app 拒启，防盘没
  挂好把数据写进系统盘。部署务必 `touch <workspace>/.mounted`；留空关闭。
- **密码**：`POSTGRES_PASSWORD`、`REDIS_PASSWORD` 都必改——两者端口都发布到宿主，redis 以
  `--requirepass` 保护。
- **资源上限**：compose 给 postgres/redis/app 设了 `deploy.resources.limits`（1g / 512m / 3g），
  按机器与负载调整。
- 沙箱由宿主 Docker 经 `docker.sock` 拉起，与 compose 服务同网络
  （`sandbox.toml` 的 `network_mode = openagentos_default`，即 `<部署目录名>_default`）。
- 迁移：`RUN_MIGRATIONS_ON_STARTUP=true` 启动自动迁移；多实例部署设 `false` 并带外
  `aegra db upgrade`。
- 共享磁盘是唯一持久真源；沙箱可弃（TTL 到期销毁，下次操作重挂同一 `subPath` 重建）。单机自托管
  假设，多节点改用 K8s + PVC（设 `AGENTOS_WORKSPACE_CLAIM`）。
