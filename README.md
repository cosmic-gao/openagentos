# OpenAgentOS

自托管的 **AI agent OS**：[DeepAgents](https://github.com/langchain-ai/deepagents)
（agent harness）跑在 [Aegra](https://github.com/aegra/aegra)（自托管 Agent Protocol
服务器）之上。你的基础设施、你的数据、零厂商锁定。

- **DeepAgents** 负责造 agent —— 规划（`write_todos`）、虚拟文件系统、子代理、skills、
  human-in-the-loop，全部构建在 LangGraph 之上。
- **Aegra** 负责托管 —— Agent Protocol API、PostgreSQL 持久化、流式、cron、可插拔鉴权。
  它是 LangGraph Platform / LangSmith Deployments 的直接替代品，兼容标准 LangGraph SDK、
  Agent Chat UI、LangGraph Studio、AG-UI / CopilotKit。

`create_deep_agent(...)` 返回一个已编译的 LangGraph 图；Aegra 托管该图并在运行时注入
持久化。这就是核心思路。

## 架构

```
客户端 (LangGraph SDK / Agent Chat UI / CopilotKit)
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
(检查点 / store)   (Azure / LiteLLM / …)    (每线程临时沙箱 + 共享磁盘卷)
```

## 共享磁盘布局

一块盘（host bind 或 K8s PVC）同时挂给 app 与所有沙箱，是全部持久文件的唯一真源：

```
workspace/                          ← AGENTOS_WORKSPACE
├── .deepagent/<assistant_id>/      ← 助手资产（app 管理）
│   ├── skills/                     → 挂到该助手每个沙箱的 /workspace/skills
│   └── .mcp.json                   → graph 工厂读取，载入 MCP 工具
└── <assistant_id>/<thread_id>/     ← 线程持久文件
                                    → 挂到该线程沙箱的 /workspace（沙箱销毁后仍在）
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
[`make_graph(config)`](agentos/graph.py)，Aegra 每次请求用该 assistant 的 `config`
（含 `assistant_id`）调用它；工厂读共享磁盘上的 `.mcp.json` 载入 MCP 工具，把
`/workspace/skills` 交给 deepagents 的 `SkillsMiddleware` 渐进式加载。

### 启动 OpenSandbox 服务器

沙箱需要一个 OpenSandbox 服务（依赖 Docker）：

```bash
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
uvx opensandbox-server          # 默认 localhost:8080
```

相关环境变量见 [.env.example](.env.example)（`OPEN_SANDBOX_DOMAIN`、`AGENTOS_SANDBOX_*`）。
不需要执行能力时设 `AGENTOS_SANDBOX_ENABLED=false`：回退 `StateBackend`（无需服务器，也无
`execute` 工具）。

## 版本锁定

| 组件 | 版本 |
| -------------------- | --------- |
| aegra-cli / aegra-api| 0.9.24    |
| deepagents           | 0.6.12    |
| langchain / -core    | 1.3.11 / 1.4.8 |
| langgraph            | 1.2.8     |
| langchain-openai     | 1.3.3     |
| langchain-mcp-adapters | 0.3.0   |
| deepagents-opensandbox | git @ cosmic-gao/opensandbox |
| opensandbox          | >= 0.1.13 |
| Python               | 3.12（由 `.python-version` 锁定） |

> 仓库锁定 Python 3.12 以获得最广的原生 wheel 覆盖。即使系统 Python 更新，`uv` 也会
> 自动获取 3.12。

## 前置依赖

- [`uv`](https://docs.astral.sh/uv/)（依赖与 Python 版本管理）
- **Docker**（Aegra 会自动拉起 PostgreSQL）。运行服务器前先启动 Docker Desktop。
- 一个 **OpenAI 兼容的 LLM 网关**（URL + key）—— 如 MSPbots 网关、Azure OpenAI、
  LiteLLM、vLLM 或 Ollama。
- 若需要沙箱内 `execute`：一个 **OpenSandbox 服务器**（`uvx opensandbox-server`，基于
  Docker）—— 见上文「沙箱与隔离」。可选；设 `AGENTOS_SANDBOX_ENABLED=false` 可跳过。

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
│   ├── workspace.py        # 共享磁盘布局（.deepagent/<aid>/ 与 <aid>/<tid>/）
│   ├── model.py            # OpenAI 兼容网关 chat model 工厂（model.build）
│   ├── mcp.py              # 从 .mcp.json 载入 MCP 工具（parse / tools）
│   ├── tools.py            # internet_search（Tavily）+ share_file（共享磁盘直链）
│   ├── sandbox.py          # 每线程临时沙箱：发现/恢复/重建（SessionSandbox）
│   ├── builder.py          # 组装：model + tools + subagents → create_deep_agent
│   ├── graph.py            # 异步工厂 make_graph(config) 按 assistant 构图  ← 入口
│   └── routes.py           # Aegra 自定义 HTTP：/files/{aid}/{tid}/{path} 下载
└── scripts/
    └── smoke_test.py       # 用 LangGraph SDK 流式跑一轮
```

## 配置

### `aegra.json`

与 `langgraph.json` 同构，这里保持最简：

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
    "assistant_id": "finance-bot"
  }
}
```

model/api_key/base_url 缺项回退全局 `OPENAI_*` env；prompt 缺省回退内置 `SYSTEM_PROMPT`。
MCP 与 skills 不在 config 里——放共享磁盘 `workspace/.deepagent/<assistant_id>/`
（见「定制 agent」）。

### 环境变量（`.env`，全局兜底）

| 变量 | 用途 |
| ------------------- | --------------------------------------------------- |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | 全局网关兜底（每助手可在 assistant config 覆盖） |
| `TAVILY_API_KEY`    | 可选，为 `research-agent` 开启联网搜索 |
| `AGENTOS_WORKSPACE` | 共享磁盘根（app 视角，默认 `workspace`） |
| `AGENTOS_WORKSPACE_HOST` | 沙箱 bind mount 用宿主绝对路径（缺省=上者绝对路径） |
| `AGENTOS_WORKSPACE_CLAIM` | K8s PVC claim（设了则优先于 host 路径） |
| `AGENTOS_PUBLIC_URL` | 下载链接前缀（`share_file` / `/files` 路由） |
| `AGENTOS_SANDBOX_ENABLED` | 是否启用沙箱（默认 `true`；`false` 回退 StateBackend） |
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
// workspace/.deepagent/<assistant_id>/.mcp.json
{
  "mcpServers": {
    "fs":      { "transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"] },
    "mspbots": { "transport": "streamable_http", "url": "https://your-host/mcp", "headers": { "Authorization": "Bearer ..." } }
  }
}
```

兼容 Claude/Cursor 风格：可用 `type`（`stdio`/`sse`/`http`）代替 `transport`，或省略（据
`command`/`url` 自动推断）。

**换模型** — 每助手在 assistant config 设 `model`/`base_url`，或改全局 `OPENAI_*` env。
若想直接用 Anthropic/Google 而非网关，把 `agentos/model.py` 里的 `ChatOpenAI` 换成
`init_chat_model("anthropic:...")`（deepagents 与模型无关）。

## 持久化、记忆与鉴权

- **持久化**由 Aegra 负责：它在运行时注入 PostgreSQL 的 checkpointer/store，所以
  `graph.py` 不传 `checkpointer`/`store`。线程与 run 重启后自动保留。
- **长期记忆 / 语义 store**：在 `aegra.json` 加 `store` 块（见配置参考）以开启跨线程的
  向量记忆。
- **鉴权**默认无（开发态）。在 `aegra.json` 加 `auth` 块以启用 JWT/OAuth/Firebase ——
  <https://docs.aegra.dev/guides/authentication>。

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
