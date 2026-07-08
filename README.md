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
            • research-agent 子代理
            • backend：每线程临时沙箱（execute；/workspace 挂共享磁盘）
   │                  │                     │
   ▼                  ▼                     ▼
PostgreSQL        OpenAI 兼容网关          OpenSandbox 容器服务
(检查点 / store)   (URL + key)              (每线程临时沙箱 + 共享磁盘卷)
```

## 共享磁盘布局

一块盘（host bind 或 K8s PVC）同时挂给 app 与所有沙箱，是全部持久文件的唯一真源：

```
workspace/                          ← AGENTOS_WORKSPACE
├── .deepagent/<assistant_id>/      ← 助手资产（app 管理）
│   ├── skills/                     → 挂到该助手每个沙箱的 /workspace/skills
│   └── .mcp.json                   → graph 工厂读取，载入 MCP 工具
└── sandbox/<thread_id>/            ← 会话沙箱文件（只按 thread 分区，不绑 assistant）
    ├── storage/                    → 挂到该线程沙箱的 /workspace（持久，沙箱销毁后仍在）
    └── tmp/                        → 挂到该线程沙箱的 /tmp（临时）
```

## 沙箱与隔离

复用开源包，不自研：

- **每线程临时沙箱（execute）** — 每个 (assistant, thread) 一个
  [OpenSandbox](https://github.com/cosmic-gao/opensandbox) 容器沙箱，桥接由开源包
  `deepagents-opensandbox` 提供。生命周期归服务端：创建时带
  `timeout=AGENTOS_SANDBOX_TTL`，到期自动销毁；进程内只缓存句柄，操作时按 metadata
  发现已有沙箱（RUNNING 连接 / PAUSED 恢复），失联则重建并重试一次
  （[agentos/sandbox.py](agentos/sandbox.py)）。**持久化在共享磁盘上，沙箱本身可弃**——
  重建后按同一 subPath 重挂，线程文件无损。
- **每助手隔离靠磁盘 subPath** — 沙箱挂两个卷：`<aid>/<tid>` → `/workspace`（线程私有），
  `.deepagent/<aid>/skills` → `/workspace/skills`（助手级，跨线程共享）。助手间互不可见。

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

# 4. 另开一个终端，发送一条消息
uv run python scripts/smoke_test.py
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
│   ├── config.py           # Settings(env) + AgentConfig(configurable) + resolve + 系统提示
│   ├── workspace.py        # 共享磁盘布局（.deepagent/<aid>/ 与 sandbox/<tid>/）
│   ├── model.py            # OpenAI 兼容网关 chat model 工厂（model.build）
│   ├── mcp.py              # 从 .mcp.json 载入 MCP 工具（parse / tools）
│   ├── tools.py            # internet_search（Tavily）+ download_file（共享磁盘直链）
│   ├── sandbox.py          # 每线程临时沙箱：发现/恢复/重建（SessionSandbox）
│   ├── builder.py          # 组装：model + tools + subagents → create_deep_agent
│   ├── graph.py            # 异步工厂 make_graph(config) 按 assistant 构图  ← 入口
│   ├── assets.py           # .deepagent/<aid>/ 资产文件的增删改移（限定目录内）
│   ├── auth.py             # 自定义鉴权：从 x-tenant-id / x-user-id 头解析身份
│   └── routes.py           # Aegra 自定义 HTTP：线程文件下载 + /assistants/{aid} 资产管理
└── scripts/
    └── smoke_test.py       # 用 LangGraph SDK 流式跑一轮
```

## 配置

### `aegra.json`

保持最简：

```json
{
  "dependencies": ["."],
  "graphs": { "agentos": "./agentos/graph.py:make_graph" },
  "http": { "app": "./agentos/routes.py:app", "enable_custom_route_auth": true }
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
    "interrupt_on": { "execute": true },
    "steps": 40,
    "fallback_model": "gpt-4o-mini",
    "pii_strategy": "redact",
    "permission": { "execute": "ask", "write_file": "deny" }
  }
}
```

model/api_key/base_url 缺项回退全局 `OPENAI_*` env；prompt 缺省回退内置 `SYSTEM_PROMPT`。
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
默认 2、`AGENTOS_TOOL_CALL_LIMIT`、`AGENTOS_FALLBACK_MODEL`、`AGENTOS_PII_STRATEGY`、`AGENTOS_CONTEXT_EDITING`
默认开、`AGENTOS_TOOL_SELECTOR_MAX`）；每助手可在 config 覆盖 `steps`（每 run 模型调用上限）、`fallback_model`、
`pii_strategy`（`off`/`redact`/`mask`/`hash`/`block`）、`review.rubric`（配了即启用自审迭代）。装配见
[agentos/middleware.py](agentos/middleware.py)。

**工具治理（每助手，可选）**：`tools`（`{工具: false}` 禁用）与 `permission`（`allow`/`ask`/`deny`）——
`deny`/禁用经官方 `wrap_model_call` 对模型隐藏该工具，`ask` 并入 `interrupt_on`（HITL 审批）。友好名
`bash`/`read`/`write`/`edit` 自动映射为 `execute`/`read_file`/`write_file`/`edit_file`。

**原生 Anthropic（可选）**：`model` 用 `anthropic:` 前缀（如 `anthropic:claude-sonnet-4-5`）走
`langchain-anthropic`，激活默认栈里的 `AnthropicPromptCachingMiddleware`（静态段缓存）；`base_url` 须指向
**Anthropic 协议**端点（网关需开 Anthropic 直通口，OpenAI 格式口不行）。缺省仍走 `ChatOpenAI` 网关。

### 环境变量（`.env`，全局兜底）

| 变量 | 用途 |
| ------------------- | --------------------------------------------------- |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | 全局网关兜底（每助手可在 assistant config 覆盖） |
| `TAVILY_API_KEY`    | 可选，为 `research-agent` 开启联网搜索 |
| `AGENTOS_WORKSPACE` | 共享磁盘根（app 视角，默认 `workspace`） |
| `AGENTOS_WORKSPACE_HOST` | 沙箱 bind mount 用宿主绝对路径（缺省=上者绝对路径） |
| `AGENTOS_WORKSPACE_CLAIM` | K8s PVC claim（设了则优先于 host 路径） |
| `AGENTOS_PUBLIC_URL` | 下载链接前缀（`download_file` / `/files` 路由） |
| `AGENTOS_MEMORY_ENABLED` | 是否启用长期记忆（默认 `true`；`/memories/` 路由到持久 store） |
| `OPEN_SANDBOX_DOMAIN` / `OPEN_SANDBOX_API_KEY` | OpenSandbox 服务器地址 / 鉴权 |
| `AGENTOS_SANDBOX_IMAGE` | 沙箱镜像（默认 `python:3.12`） |
| `AGENTOS_SANDBOX_TTL` | 沙箱寿命秒数（默认 `1800`，到期服务端销毁） |
| `AGENTOS_SANDBOX_TIMEOUT` | 可选，每条命令默认超时（秒） |
| `AGENTOS_SERVER_PROXY` | 经 OpenSandbox 服务器代理转发（compose 常态，默认 `true`） |

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

也可用 `type`（`sse` / `http`）代替 `transport`，或省略（据 `url` 自动推断为
`streamable_http`）。

> ⚠️ **仅允许 `streamable_http` / `sse`（远程 http 家族）**：`stdio`（本地子进程）、`websocket` 及无法
> 推断 transport 的条目会被**忽略并告警**——服务端不宜起子进程，且 app 镜像不含 `node`/`uv`。
> 策略集中在 [agentos/mcp.py](agentos/mcp.py) 的 `_ALLOWED_TRANSPORTS`——确需放开某类，改这一处即可。

**换模型** — 每助手在 assistant config 设 `model`/`base_url`，或改全局 `OPENAI_*` env。
若想直接用 Anthropic/Google 而非网关，把 `agentos/model.py` 里的 `ChatOpenAI` 换成
`init_chat_model("anthropic:...")`（deepagents 与模型无关）。

## 持久化、记忆与鉴权

- **持久化**由 Aegra 负责：它在运行时注入 PostgreSQL 的 checkpointer/store，所以
  `graph.py` 不传 `checkpointer`/`store`。线程与 run 重启后自动保留。
- **长期记忆**（默认开启，`AGENTOS_MEMORY_ENABLED`）：经 `CompositeBackend` 把虚拟路径
  `/memories/` 路由到 `StoreBackend`（落 Aegra 的 PostgreSQL store），**跨该助手所有线程持久**
  （namespace 按 `assistant_id` 隔离）。启动时加载 `/memories/AGENTS.md`，agent 用 `edit_file`
  自维护记忆实现跨会话学习。`/workspace`（线程私有）与 `/memories/`（助手级持久）各司其职。
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
  > ⚠️ **同理，`routes.py` 的自定义资产/文件路由只认证、不复核归属**：任何已认证调用者拿到
  > `assistant_id`（`/assistants/{id}/files…`）或 `thread_id`（`/files/{thread_id}/…`）即可读写对应文件；
  > 默认 `system` assistant 更是人人可见。这是**刻意设计**——归属由上游可信网关保证。要在本服务内强制，
  > 加 `@auth.on` 处理器或在 `routes.py` 里按 `user` 校验资源归属。

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
```
