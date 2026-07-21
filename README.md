# OpenAgentOS

自托管的 **AI agent OS**：[DeepAgents](https://github.com/langchain-ai/deepagents)
（agent harness）跑在 [Aegra](https://github.com/aegra/aegra)（自托管 Agent Protocol
服务器）之上。

- **DeepAgents** 负责造 agent —— 规划（`write_todos`）、虚拟文件系统、子代理、skills、
  human-in-the-loop，全部构建在 LangGraph 之上。
- **Aegra** 负责托管 —— Agent Protocol API、PostgreSQL 持久化、流式、cron、可插拔鉴权。

`create_deep_agent(...)` 返回一个已编译的 LangGraph 图；Aegra 托管该图并在运行时注入
持久化。这就是核心思路。

## 架构

```
客户端 (LangGraph SDK)
   │  Agent Protocol (HTTP + SSE)
   ▼
Aegra 服务器 (FastAPI, :2026)
   • assistants / threads / runs / crons
   • 持久化 + 流式 + 鉴权
   • graph "agentos"  (agentos/graph.py)
       └─ create_deep_agent(..., backend=…)      ← DeepAgents harness
            • 规划 write_todos + 文件系统 + skills
            • backend：每线程临时沙箱（execute；/workspace 挂共享磁盘）
   │                  │                     │
   ▼                  ▼                     ▼
PostgreSQL        OpenAI 兼容网关          OpenSandbox 容器服务
(检查点 / store)   (URL + key)              (每线程临时沙箱 + 共享磁盘卷)
```

## 共享磁盘布局

一块盘（host bind 或 K8s PVC）挂给 app 与所有沙箱，只存**助手级配置**（skills / MCP）；会话文件不落盘：

```
workspace/                          ← AGENTOS_WORKSPACE
└── .deepagent/<assistant_id>/      ← 助手资产（app 管理，按 assistant 隔离）
    ├── skills/                     → 只读挂到该助手每个沙箱的 /workspace/skills
    └── .mcp.json                   → graph 工厂读取，载入 MCP 工具
# 会话 /workspace 落沙箱容器本地、随箱销毁（ephemeral，与 LGP 一致）——
# 要长期留存写 /memories/（→ Store/Postgres）；要交付给用户用 download_file（拷入 Store，见下）。
```

## 沙箱与隔离

复用开源包，不自研：

- **每线程临时沙箱（execute）** — 每个 (assistant, thread) 一个
  [OpenSandbox](https://github.com/cosmic-gao/opensandbox) 容器沙箱，桥接由开源包
  `deepagents-opensandbox` 提供。生命周期归服务端：创建时带
  `timeout=AGENTOS_SANDBOX_TTL`，到期自动销毁；进程内只缓存句柄，操作时按 metadata
  发现已有沙箱（RUNNING 连接 / PAUSED 恢复），失联则重建并重试一次
  （[agentos/sandbox/session.py](agentos/sandbox/session.py)）。**会话 `/workspace` 是 ephemeral**——
  沙箱销毁/重建后为全新空目录（与 LGP 一致）；要持久的数据走 `/memories/`（Store），要交付的文件用
  `download_file`（拷入 Store）。
- **每助手隔离** — 沙箱只挂 skills 一个卷：`.deepagent/<aid>/skills` → `/workspace/skills`
  （助手级、跨线程共享的只读配置）；会话 `/workspace` 是容器本地、按 (assistant, thread) 独立。助手间互不可见。

**按 assistant 构图（graph 工厂）**：aegra.json 指向异步工厂
[`make_graph(config, runtime)`](agentos/graph.py)，Aegra 每次请求用该 assistant 的 `config`
调用它；工厂读共享磁盘上的 `.mcp.json` 载入 MCP 工具，把 `/workspace/skills` 交给 deepagents
的 `SkillsMiddleware` 渐进式加载。工厂**区分 introspection 与执行**：仅真正执行
（`runtime.execution_runtime` 非 None）时才连 MCP、写盘；schema / 画图等只读调用走轻量路径
（不连 MCP、不写盘），图拓扑保持一致。

### 启动 OpenSandbox 服务器

沙箱需要一个 OpenSandbox 服务（依赖 Docker）：

```bash
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
uvx opensandbox-server          # 默认 localhost:8080
```

相关环境变量见 [.env.example](.env.example)（`OPEN_SANDBOX_DOMAIN`、`AGENTOS_SANDBOX_*`）。

## 前置依赖

- [`uv`](https://docs.astral.sh/uv/)（依赖与 Python 版本管理）
- **Docker**（Aegra 会自动拉起 PostgreSQL）。运行服务器前先启动 Docker Desktop。
- 一个 **OpenAI 兼容的 LLM 网关**（URL + key）。
- 一个 **OpenSandbox 服务器**（`uvx opensandbox-server`，基于 Docker）提供沙箱内 `execute`
  —— 见上文「沙箱与隔离」。

## 快速开始

```bash
# 1. 配置网关
cp .env.example .env
#    → 编辑 .env：设置 OPENAI_BASE_URL、OPENAI_API_KEY、OPENAI_MODEL

# 2. 安装依赖（创建使用 Python 3.12 的 .venv）
uv sync

# 3. 启动 Docker Desktop，然后运行开发服务器
uv run aegra dev
#    → API 与文档：http://localhost:2026/docs
#    → PostgreSQL 会自动启动

# 4. 打开 http://localhost:2026/docs 试跑：创建 assistant → thread → run 发一条消息
#    （或用 LangGraph SDK 指向 http://localhost:2026 编程调用）
```

若首次运行报缺少数据库表，显式执行迁移：

```bash
uv run aegra db upgrade
```

## 项目结构

```
openagentos/
├── aegra.json              # 向 Aegra 注册 "agentos" 图
├── pyproject.toml          # uv 项目 + 锁定依赖
├── .python-version         # 3.12
├── .env.example            # 网关 + 可选 key 模板
├── agentos/                # agent 本体（Python 包）
│   ├── __init__.py         # 加载 .env + 公共 API 导出
│   ├── config.py           # Settings(env) + AgentConfig(configurable) + resolve
│   ├── prompts.py          # 系统提示词常量（主 agent 默认提示 / harness 后缀）
│   ├── workspace.py        # 共享磁盘布局（.deepagent/<aid>/ 助手配置）
│   ├── model.py            # OpenAI 兼容网关 chat model 工厂（model.build）
│   ├── mcp.py              # 从 .mcp.json 载入 MCP 工具（parse / tools）
│   ├── tools.py            # download_file（把交付物拷入 Store、给下载链接）
│   ├── artifacts.py        # 交付物存 Store（download_file 与下载路由共用）
│   ├── sandbox/            # 每线程临时沙箱 + 一次性执行
│   │   ├── client.py       #   OpenSandbox 连接层与沙箱规格（共享）
│   │   ├── session.py      #   会话沙箱：发现/恢复/续期/重建（SessionSandbox）
│   │   └── run.py          #   一次性无状态执行（供 /sandboxes/execute）
│   ├── middleware.py       # 官方中间件栈装配（重试/上限/回退/裁剪/PII/密钥脱敏）+ ToolFilter
│   ├── review.py           # rubric 自审子系统（意图路由 + 迭代评分 + 裁决 UI + score 上报）
│   ├── redaction.py        # 密钥/凭据兜底脱敏（PIIMiddleware callable detector）
│   ├── scoring.py          # Langfuse score 上报（rubric 裁决 / 用户反馈 → trace）
│   ├── builder.py          # 组装：model + tools + subagents + 中间件 → create_deep_agent
│   ├── graph.py            # 异步工厂 make_graph(config) 按 assistant 构图  ← 入口
│   ├── assets.py           # .deepagent/<aid>/ 资产文件的增删改移（限定目录内）
│   ├── auth.py             # 自定义鉴权：从 x-tenant-id / x-user-id 头解析身份
│   ├── msteams.py          # MS Teams 通道：每 agent 一个 bot，Bot Framework webhook ⇄ Agent Protocol 桥接
│   └── routes/             # Aegra 自定义 HTTP（各资源一个 APIRouter，平铺挂根 app）
│       ├── __init__.py     #   app 装配 + lifespan（含 msteams 预热/收尾）+ HTTPException handler
│       ├── common.py       #   领域异常 → HTTP 码映射（共享）
│       ├── files.py        #   会话交付物下载（从 Store 读回）
│       ├── execute.py      #   一次性沙箱执行 code
│       ├── assets.py       #   助手资产 CRUD + 跨助手批量
│       └── feedback.py     #   用户反馈 → Langfuse score
└── tests/                  # pytest 单元测试（config/workspace/assets/redaction/mcp/tools 纯函数）
```

## 配置

### `aegra.json`

保持最简：

```json
{
  "dependencies": ["."],
  "graphs": { "agentos": "./agentos/graph.py:make_graph" },
  "http": { "app": "agentos.routes:app", "enable_custom_route_auth": true }
}
```

Aegra 还支持的可选键：`auth`（JWT/OAuth/Firebase/自定义）、`store`（带向量嵌入的语义
store）。见 <https://docs.aegra.dev/reference/configuration>。

### 每助手配置（assistant `config`）

创建 assistant 时把该助手的设置放进 `config.configurable`，Aegra 在 run 时并入、工厂读取：

```jsonc
// POST /assistants 的 config 字段
{
  "configurable": {
    "model": "gpt-4o",
    "prompt": "You are ...",
    "api_key": "sk-...",
    "base_url": "https://your-gateway/v1",
    "assistant_id": "finance-bot",
    "context_window": 131072,
    "stream_usage": true,
    "interrupt_on": { "execute": true },
    "steps": 40,
    "fallback_model": "gpt-4o-mini",
    "pii_strategy": "redact",
    "permission": { "execute": "ask", "write_file": "deny" }
  }
}
```

model/api_key/base_url/context_window/stream_usage 缺项回退全局 env（`OPENAI_*` /
`AGENTOS_CONTEXT_WINDOW` / `AGENTOS_STREAM_USAGE`）；prompt 缺省回退内置 `SYSTEM_PROMPT`。`context_window`
供网关自定义模型名声明真实上下文窗口，使自动摘要压缩按"窗口 85%"提前触发（缺省则 langchain 按模型名推断，
认不出时退回固定 170k 阈值）；`stream_usage` 使流式响应回传 token usage（Langfuse 成本统计所需）。
MCP 与 skills 不在 config 里——放共享磁盘 `workspace/.deepagent/<assistant_id>/`
（见「定制 agent」）。

**`assistant_id`（重要）**：Aegra 不会自动把它注入 configurable，缺省即回退到共享的
`default` 命名空间（skills / .mcp.json / 资产 / 记忆都会被多个助手混用）。**多助手部署务必
为每个助手在 config 里显式设 `assistant_id`**，且与资产 API URL 里用的值一致。

**`interrupt_on`（human-in-the-loop，可选）**：`{工具名: true}` 或
`{工具名: {"allowed_decisions": ["approve","edit","reject"], "description": "..."}}`。命中的
工具在调用前挂起，等客户端用 `Command(resume=...)` 决策后继续（需 checkpointer——Aegra 已
默认注入）。缺省不中断。常用于对 `execute`（沙箱执行 shell）设人工审批。

**官方中间件（韧性/成本/合规/上下文，可选）**：追加 LangChain / deepagents 官方中间件到默认栈之后（不替换
skills/memory/summarization）。全局默认在 `.env`（`AGENTOS_MODEL_MAX_RETRIES` / `AGENTOS_TOOL_MAX_RETRIES`
默认 2、`AGENTOS_TOOL_CALL_LIMIT`、`AGENTOS_FALLBACK_MODEL`、`AGENTOS_PII_STRATEGY`、`AGENTOS_SECRET_REDACTION`
默认开、`AGENTOS_TOOL_SELECTOR_MAX`）；每助手可在 config 覆盖 `steps`（每 run 模型调用上限）、`fallback_model`、
`pii_strategy`（`off`/`redact`/`mask`/`hash`/`block`）、`review.rules`（配了即启用自审迭代：规则表按对话内容
逐 run 路由到对应 rubric——每条 `{name, rubric, triggers?, description?}`，`triggers` 正则命中优先、否则
`review.gate=true` 时用 `review.model` 从规则表选一条，都不中则跳过，`review.gate_prompt` 可覆盖路由标准）。
中间件栈装配见 [agentos/middleware.py](agentos/middleware.py)，自审子系统见 [agentos/review.py](agentos/review.py)。

**工具治理（每助手，可选）**：`tools`（`{工具: false}` 禁用）与 `permission`（`allow`/`ask`/`deny`）——
`deny`/禁用经官方 `wrap_model_call` 对模型隐藏该工具，`ask` 并入 `interrupt_on`（HITL 审批）。工具名
用真实名（`execute`/`read_file`/`write_file`/`edit_file`/`ls`/`glob`/`grep`/`write_todos`/`task` 等）。

**原生 Anthropic（可选）**：`model` 用 `anthropic:` 前缀（如 `anthropic:claude-sonnet-4-5`）走
`langchain-anthropic`，激活默认栈里的 `AnthropicPromptCachingMiddleware`（静态段缓存）；`base_url` 须指向
**Anthropic 协议**端点（网关需开 Anthropic 直通口，OpenAI 格式口不行）。缺省仍走 `ChatOpenAI` 网关。

### 环境变量（`.env`，全局兜底）

| 变量 | 用途 |
| ------------------- | --------------------------------------------------- |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | 全局网关兜底（每助手可在 assistant config 覆盖） |
| `AGENTOS_CONTEXT_WINDOW` | 模型上下文窗口 tokens；网关自定义模型名须显式设置，否则自动摘要压缩退回固定 170k 阈值（每助手可覆盖 `context_window`） |
| `AGENTOS_STREAM_USAGE` | 默认 `true`；流式请求带 `stream_options.include_usage`，使流式响应回传 token usage（Langfuse 成本统计所需）。个别严格网关拒绝该参数则设 `false`（每助手可覆盖 `stream_usage`） |
| `AGENTOS_WORKSPACE` | 共享磁盘根（app 视角，默认 `workspace`） |
| `AGENTOS_WORKSPACE_HOST` | 沙箱 bind mount 用宿主绝对路径（缺省=上者绝对路径） |
| `AGENTOS_WORKSPACE_CLAIM` | K8s PVC claim（设了则优先于 host 路径） |
| `AGENTOS_PUBLIC_URL` | 下载链接前缀（`download_file` / `/files` 路由） |
| `AGENTOS_MEMORY_ENABLED` | 是否启用长期记忆（默认 `true`；`/memories/` 路由到持久 store） |
| `OTEL_TARGETS` | 可观测性总开关（默认空=关；设 `LANGFUSE` 上报 token/耗时/成本到 Langfuse） |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | Langfuse 项目 key 与地址（见「可观测性」） |
| `MSTEAMS_LOCAL_URL` | 可选,MS Teams 通道读 assistant 配置/跑 run 的本地 Agent Protocol 地址(默认 `http://localhost:2026`;bot 凭据按 agent 存于 assistant 配置,不放 env,见「MS Teams 通道」) |
| `OPEN_SANDBOX_DOMAIN` / `OPEN_SANDBOX_API_KEY` | OpenSandbox 服务器地址 / 鉴权 |
| `AGENTOS_SANDBOX_IMAGE` | 沙箱镜像（默认 `python:3.12`） |
| `AGENTOS_SANDBOX_TTL` | 沙箱寿命秒数（默认 `300`，到期服务端销毁） |
| `AGENTOS_SANDBOX_TIMEOUT` | 可选，每条命令默认超时（秒） |
| `AGENTOS_SERVER_PROXY` | 经 OpenSandbox 服务器代理转发（compose 常态，默认 `true`） |

## HTTP API

Aegra custom app 路由（[agentos/routes/](agentos/routes/__init__.py)），随核心 Agent Protocol 路由挂在同一服务上，
经 `enable_custom_route_auth` 注入认证。通用约定：

- **错误体**统一为 Agent Protocol 标准 `{ "error": "<type>", "message": "…", "details": null }`
  （`error` 由状态码映射：`bad_request` / `not_found` / `conflict` / `validation_error` / …）。
- **归属鉴权与多租户隔离由上游业务层负责**，本层只认证、不复核资源归属（见「持久化、记忆与鉴权」）。
- 路径里的 `{rel}` 为目录内相对路径，越界（`..` / 绝对路径 / 符号链接逃逸）→ `400`。

### 会话交付物下载（按 (user, assistant) 隔离）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/files/{assistant_id}/{thread_id}/{rel}` | 下载会话交付物（`download_file` 交付时已把文件从 ephemeral 沙箱拷进 Store，此处从 Store 读回；沙箱销毁后仍有效）。**与记忆同粒度按 identity + assistant 隔离**：`identity` 取自鉴权（非 URL），越权/不存在 `404` |

### 一次性沙箱执行

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/sandboxes/execute` | 新建临时沙箱执行代码、完成即销毁（单次、无状态、无会话）|

请求体：`code`（必填）、`language`（`python`\|`bash`\|`sh`）、`args`、`env`、`stdin`、`params`、`timeout`（1–600s）。
响应 `{output, exit_code, truncated}`。程序非零退出仍 `200`；语言不支持 / env 名非法 → `400`，沙箱落盘失败 → `503`，入参越界 → `422`。

### 助手资产文件（按 `assistant_id` 分区，`.deepagent/<aid>/`）

| 方法 | 路径 | 说明 | 成功码 |
|---|---|---|---|
| `GET` | `/assistants/{aid}/files` | 列目录项；query `path`（子目录）、`recursive`、`limit`（1–1000，默认 100）、`offset`。返回 `{items,total,limit,offset}` | `200` |
| `GET` | `/assistants/{aid}/files/{rel}` | 读文件（**内容协商**）：默认原始字节流（按扩展名判 `Content-Type`、支持 `Range`）；`Accept: application/json` 或 `?format=json` → `{path,content}`（仅 UTF-8 文本，二进制 `415`）；`?format=raw` 强制字节流、优先于 `Accept` | `200` |
| `PUT` | `/assistants/{aid}/files/{rel}` | **幂等 upsert**：请求体为原始字节（文本即 UTF-8）。返回 `{path,size}` | `201` 新建 / `200` 覆盖 |
| `DELETE` | `/assistants/{aid}/files/{rel}` | 删除文件或目录（递归）。返回 `{path,deleted}`；不存在 `404` | `200` |
| `POST` | `/assistants/{aid}/move` | 移动 / 重命名，体 `{src,dest}`；目标已存在 `409` | `200` |
| `POST` | `/assistants/{aid}/upload` | multipart 多文件上传（各带相对路径即重建目录树）；form `dest`、`extract`（true 时 zip 解压进 dest）。返回 `{written:[…]}` | `201` |
| `GET` | `/assistants/{aid}/download` | 把 `.deepagent/<aid>/` 打包 zip 下载（跳过符号链接与 VCS 目录） | `200` |

单次上传 / PUT 体上限 100 MB、zip 解压总量上限 256 MB（best-effort；硬上限应在反代 `client_max_body_size`）。

### 跨助手批量（运维）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/files/upload` | 把同一批文件分发到多个 assistant（form `assistant_ids`、`files`、`dest`、`extract`）|
| `POST` | `/files/delete` | 从多个 assistant 删同一路径，体 `{assistant_ids, path}` |

两者均返回 **`207 Multi-Status`** + `{results:[{assistant_id, status, written?/deleted?, error?}]}`，单个失败不中断其余。

### 反馈（→ Langfuse score）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/feedback` | 把用户反馈作为 Langfuse score 关联到 trace，体 `{trace_id, value, name?, data_type?, comment?}`；`trace_id` 为该 run 的 OTEL trace id（32-hex）。缺 Langfuse 凭据则静默忽略 |

## 定制 agent

**加工具** — 在 `agentos/tools.py` 写一个带 docstring 的普通函数，并在
`agentos/graph.py` 的 `agent_tools` 里加入。它会与 DeepAgents 内置工具（`write_todos`、
`ls`、`read_file`、`write_file`、`edit_file`、`glob`、`grep`、`execute`、`task` 等）一起暴露。

**加子代理** — 往 `agentos/builder.py` 追加一个 `SubAgent` dict
（`llm` 为工厂构造的该助手模型）：

```python
{
    "name": "my-agent",
    "description": "When the main agent should delegate to me.",
    "system_prompt": "You are ...",
    "tools": [my_tool],   # 可选；覆盖继承的工具
    "model": llm,         # 可选；默认沿用该助手模型
}
```

**加 skill**（Agent Skills 规范）— 在共享磁盘
`workspace/.deepagent/<assistant_id>/skills/<name>/SKILL.md`（`<name>` 须与目录名一致；
沙箱内可见于 `/workspace/skills/<name>/`，该助手所有线程共享）：

```markdown
---
name: web-research            # 小写字母数字+连字符，≤64，须等于目录名
description: 何时用 + 做什么   # ≤1024；写清触发场景与关键词
# 可选：license、compatibility(≤500)、allowed-tools(空格分隔)、metadata
---

# web-research

具体步骤 / 最佳实践 / 示例……（可另放 scripts/、references/、assets/ 子目录）
```

`SkillsMiddleware` 渐进式加载：先给模型看 name+description，需要时再 `read_file` 全文。

**接入 MCP 工具**（QuickBooks / ClickUp / 自建）— 编辑共享磁盘
`workspace/.deepagent/<assistant_id>/.mcp.json`，graph 工厂自动载入为 tools。每个值遵循
langchain-mcp-adapters 的 Connection schema（`transport` + 对应字段）：

```jsonc
// workspace/.deepagent/<assistant_id>/.mcp.json —— 仅支持远程 server（http 家族）
{
  "mcpServers": {
    "mspbots": { "transport": "streamable_http", "url": "https://your-host/mcp", "headers": { "Authorization": "Bearer ..." } },
    "search":  { "transport": "sse", "url": "https://your-host/sse" }
  }
}
```

省略 `transport` 时据 `url` 自动推断为 `streamable_http`。

> ⚠️ **仅允许 `streamable_http` / `sse`（远程 http 家族）**：`stdio`（本地子进程）、`websocket` 及无法
> 推断 transport 的条目会被**忽略并告警**——服务端不宜起子进程，且 app 镜像不含 `node`/`uv`。
> 策略集中在 [agentos/mcp.py](agentos/mcp.py) 的 `_ALLOWED_TRANSPORTS`——确需放开某类，改这一处即可。

**换模型** — 每助手在 assistant config 设 `model`/`base_url`，或改全局 `OPENAI_*` env。
若想直接用 Anthropic/Google 而非网关，把 `agentos/model.py` 里的 `ChatOpenAI` 换成
`init_chat_model("anthropic:...")`（deepagents 与模型无关）。

## MS Teams 通道

内置的 Teams bot 通道([agentos/msteams.py](agentos/msteams.py)):用户在 Azure 创建 Bot 后,
即可在 Teams 里与 agent 一对一对话。纯 HTTP 实现(无 Bot Framework SDK),**MVP 边界:纯文本
DM 往返**——不含附件、Adaptive Card、群聊@提及、流式(Teams bot 无 SSE)、OAuth SSO。

**多租户 / 多 agent / 多 bot**:每个 agent 一个独立 Azure Bot。bot 凭据(App ID / secret / Tenant /
allowlist / 启用开关)按 agent 存于其 **assistant 配置** `config.configurable.msteams`,由**平台 UI**
(mb-platform-agent 创建/编辑 agent 的 *Microsoft Teams* 区)填写并展示每个 agent 专属的 webhook URL——
**不再放 env**。webhook 路径带该 agent 的**稳定平台 agent id**(assistant 删除重建也不变)以区分 bot。

**工作方式**:Teams → `POST /webhooks/msteams/<agentId>`(挂在 [agentos/routes/webhooks.py](agentos/routes/webhooks.py) 根 app)
→ 按 agentId 反查 assistant(`metadata.agent_id`)、以 `system` 身份读该 agent 的 bot 凭据 → 验证 Bot Framework 签名 JWT(JWKS;校验
`aud`=该 bot App ID、`iss`、要求并比对 `serviceUrl` claim)→ **立即 200 ACK**(Teams 15 秒硬超时)
→ 后台任务把消息交给本地 Agent Protocol:`thread_id = uuid5(conversation.id)` 确定性映射(同一 Teams
会话自动续同一 Aegra 线程,零映射存储)→ 以该 agent 的 assistant 跑 `runs.wait` → 取最终 AI 文本 →
出站 token(client_credentials,按 App ID 缓存 ~1h)主动 REST 回复(仅发往可信 Bot Connector 主机)。

**接入步骤**(每个 agent 重复):

1. **创建 Azure Bot**(portal.azure.com → *Azure Bot*):记下 **Microsoft App ID**、生成
   **client secret**(App Password);单租户 bot 再记下 **Tenant ID**。在 bot 的 *Channels*
   里启用 **Microsoft Teams**。
2. **在平台 UI 配置该 agent 的 Teams**:创建/编辑 agent → *Microsoft Teams* 区,打开**启用开关**,
   填 App ID / client secret /(可选)Tenant ID / 发件人 allowlist,保存。UI 会显示该 agent 的
   **webhook URL**(形如 `https://<agentos 域名>/webhooks/msteams/<agentId>?X_Tenant_ID=<租户>`)。
   > `?X_Tenant_ID=<租户>` 供**网关**把回调路由到该租户的 agentos pod——Bot Framework 回调不含平台租户
   > 上下文,故租户信息必须写进 URL。agentos 自身不用它、会剥离(单实例共享部署可忽略该参数)。
3. **配置 messaging endpoint**:把上一步显示的完整 webhook URL(含 `?X_Tenant_ID`)填进 Azure Bot 的 messaging endpoint
   (必须公网 HTTPS)。本地开发用隧道把公网 HTTPS 转发到 `:2026`:
   ```bash
   # 二选一
   devtunnel host -p 2026 --allow-anonymous
   ngrok http 2026
   # 把生成的 https://xxx/webhooks/msteams/<agentId> → 填到 Azure Bot messaging endpoint
   ```
4. **验证**:在 Teams 给 bot 发一条私聊消息(或 Azure Bot 的 *Test in Web Chat*),应先看到
   typing 提示,agent run 结束后收到回复。服务日志 `agentos.msteams` 有验签/回复的告警与错误。

**安全语义**:入站强制 JWT 验签(不可关,且必须携带并匹配 `serviceUrl` claim);配置了 Tenant ID
时对异租户/缺租户 activity **fail-closed** 丢弃;allowlist 收紧到指定发件人;`enabled` 关闭即整体停用
该 agent 的通道。凭据只存于 assistant 配置(服务端),webhook 用 `system` 身份读取。Bot Framework
回调不带 `x-tenant-id`/`x-user-id` 头,线程按 `uuid5(conversation.id)` 天然隔离。

## 持久化、记忆与鉴权

- **持久化**由 Aegra 负责：它在运行时注入 PostgreSQL 的 checkpointer/store，所以
  `graph.py` 不传 `checkpointer`/`store`。线程与 run 重启后自动保留。
- **会话回收**：会话 `/workspace` 随沙箱 TTL（`AGENTOS_SANDBOX_TTL`）销毁；thread 元数据与 checkpoint
  由 Aegra 原生 TTL 回收（`CHECKPOINTER_TTL_ENABLED`，按 idle 时长删 thread + checkpoint + runs，多副本
  分布式锁协调）——与 LGP 的 `checkpointer.ttl` 同机制，无自定义 sweeper。
- **长期记忆**（默认开启，`AGENTOS_MEMORY_ENABLED`）：经 `CompositeBackend` 把虚拟路径
  `/memories/` 路由到 `StoreBackend`（落 Aegra 的 PostgreSQL store），**按 aegra 官方 `["users", identity, assistant]`
  布局隔离——每用户每助手一份、跨该用户在该助手下的所有线程持久**（与 REST `/store` 同一棵用户树）。启动时加载 `/memories/AGENTS.md`，agent 用 `edit_file`
  自维护记忆实现跨会话学习。`/workspace`（线程私有、**ephemeral**——随沙箱销毁）与 `/memories/`
  （用户×助手级、跨线程持久）各司其职：会话产物要留存写 `/memories/`，要交付给用户用 `download_file`（拷入 Store）。
  身份(identity)由 [auth.py](agentos/auth.py) 决定，取自 `runtime.user.identity`（缺失回退 `configurable.user_id` → `anonymous`）；缺可信头时会共享 `anonymous` 记忆。
  想要向量语义检索，再在 `aegra.json` 加 `store.index` 块（见配置参考）。
- **鉴权**由 `aegra.json` 的 `auth` 块启用，指向 [agentos/auth.py](agentos/auth.py)：从请求头
  `x-tenant-id` / `x-user-id` 解析身份（`identity = <tenant>:<user>`）。**缺头不拒绝**——回退
  `default` / `anonymous`，故当前 auth 只做**身份标注（审计）**、不做强制。要"缺头即 401"，把 auth.py 的回退
  改成抛 `Auth.exceptions.HTTPException(status_code=401)`。改用 JWT/OAuth/Firebase 见
  <https://docs.aegra.dev/guides/authentication>。
  > ⚠️ **数据隔离的键是 `assistant_id` + `thread_id`，与这两个头无关**：磁盘布局、记忆 namespace、
  > 沙箱 metadata 只用这两个 id；`x-tenant-id` / `x-user-id` 仅填充身份、不参与分区。本项目把
  > 「租户↔assistant、用户↔thread」的归属放在**上游业务层**：`thread_id` 由 Aegra 服务端强制、
  > 客户端改不动；但 `assistant_id` 不被钉死（run 请求体可覆盖），故上游须**权威写入 `assistant_id`
  > 并防止客户端覆盖**。
  >
  > ⚠️ **同理，`routes/` 的自定义资产/文件路由只认证、不复核归属**：任何已认证调用者拿到
  > `assistant_id`（`/assistants/{id}/files…`）或 `thread_id`（`/files/{thread_id}/…`）即可读写对应文件；
  > 默认 `system` assistant 更是人人可见。这是**刻意设计**——归属由上游可信网关保证。要在本服务内强制，
  > 加 `@auth.on` 处理器或在 `routes/` 里按 `user` 校验资源归属。

## 可观测性(OTEL / Langfuse)

Aegra 内置 OpenTelemetry + OpenInference 自动埋点,开启后**每次 LLM 调用的 token 数、耗时、模型名**
都作为 span 上报,Langfuse 按 trace/session 聚合并算成本——这是拿到「每个 run 消耗多少 token、花了多久」
的推荐方式(run 对象本身不返回聚合用量)。默认**关闭**(`OTEL_TARGETS` 空)。

- **自托管**:`docker-compose.yml` 已带一个 opt-in 的 `langfuse` profile(6 个专用服务,与主栈隔离)。
  填好 `.env` 密钥、设 `OTEL_TARGETS=LANGFUSE`,`docker compose --profile langfuse up -d` 即可,
  UI 在 `http://localhost:3000`。完整步骤见 [DEPLOY.md](DEPLOY.md#可观测性otel--langfuseopt-in)。
- **外部 Langfuse / 云**:跳过 profile,只在 `.env` 设 `OTEL_TARGETS=LANGFUSE` + `LANGFUSE_BASE_URL` +
  项目 key 即可。
- 也支持 Phoenix / 通用 OTLP target(`OTEL_TARGETS=PHOENIX` 等),配置见 Aegra 文档。
- **质量 score**:rubric 自审裁决(每 run)与 `/feedback` 用户反馈作为 score 上报,复用同一套 `LANGFUSE_*` 凭据、按 OTEL trace_id 关联到对应 trace(缺凭据则静默禁用,不影响 run)。score 是 Langfuse 一等对象、无法走 OTEL span attribute,故经其 Public API(`POST /api/public/scores`,HTTP Basic)**直连上报**——不引 Langfuse SDK、不碰 OTEL TracerProvider;上报走后台线程池 fire-and-forget,不阻塞事件循环。

## 部署

```bash
uv run aegra up        # 构建并启动容器（Postgres + API）；首次运行会自动生成
                       # docker-compose.yml 与 Dockerfile
uv run aegra db upgrade
uv run aegra down      # 停止（加 -v 删除数据卷）
```

`aegra serve` 运行生产服务器（无热重载）。PaaS / Kubernetes 见
<https://docs.aegra.dev/guides/deployment>。

## 参考

- Aegra — <https://github.com/aegra/aegra> · 文档 <https://docs.aegra.dev>
- DeepAgents — <https://github.com/langchain-ai/deepagents> · 文档
  <https://docs.langchain.com/oss/python/deepagents/overview>
- OpenSandbox 桥接 — <https://github.com/cosmic-gao/opensandbox>
- Agent Protocol / LangGraph SDK — 直接指向 `http://localhost:2026` 使用
