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
            • 规划 write_todos + 虚拟文件系统
            • research-agent 子代理
            • backend：每线程沙箱 (execute) + 每助手 /assistant/ 目录
   │                  │                     │
   ▼                  ▼                     ▼
PostgreSQL        OpenAI 兼容网关          OpenSandbox 容器服务
(检查点 / store)   (MSPbots / Azure / …)    (每线程临时沙箱)
```

## 沙箱与隔离

在基座之上，OpenAgentOS 增加了两层运行时隔离（复用开源包，不自研）：

- **每线程临时沙箱（execute）** — 每个 Aegra thread 首次执行命令时惰性创建一个
  [OpenSandbox](https://github.com/cosmic-gao/opensandbox) 容器沙箱并在该线程内复用；
  空闲超过 `AGENTOS_SANDBOX_IDLE_TTL` 秒后由后台 reaper 销毁（OpenSandbox 的 `timeout`
  作服务端兜底）。桥接由开源包 `deepagents-opensandbox` 提供，openagentos 只加一层按
  thread 复用的多路复用后端 `SessionSandbox`（[agentos/sandbox.py](agentos/sandbox.py)）。
- **每助手磁盘目录** — 每个 assistant 按 `assistantId` 在 `.deepagent/<assistant_id>/`
  拥有独立目录：`skills/`、`.mcp.json`（MCP 服务器）、`config.json`（该助手的
  `OPENAI_BASE_URL/API_KEY/MODEL`，缺项回退全局 env）。见
  [agentos/workspace.py](agentos/workspace.py)。

**按 assistant 构图（graph 工厂）**：aegra.json 指向异步工厂
[`make_graph(config)`](agentos/graph.py)，Aegra 每次请求用该 assistant 的 config 调用它，
据此装配 **model**（`config.json`）、**skills**（`/assistant/skills`）、**MCP tools**
（`.mcp.json`）。backend 用 deepagents 的 `CompositeBackend` 组合：`default` = 每线程沙箱
（含 `execute`），`/assistant/skills/` 路由 = 该助手 `skills/` 目录（供 SkillsMiddleware
渐进式加载）；沙箱按 `thread_id` 运行时解析（[agentos/backends.py](agentos/backends.py)）。

> 安全：`config.json`（含密钥）与 `.mcp.json` 只由工厂读取，**不**进 agent 文件系统——
> 只有 `skills/` 经 `/assistant/skills/` 暴露给 agent，密钥不泄露给模型。

### 启动 OpenSandbox 服务器

沙箱需要一个 OpenSandbox 服务（依赖 Docker）：

```bash
uvx opensandbox-server init-config ~/.sandbox.toml --example docker
uvx opensandbox-server          # 默认 localhost:8080
```

相关环境变量见 [.env.example](.env.example)（`OPEN_SANDBOX_DOMAIN`、`AGENTOS_SANDBOX_*`、
`AGENTOS_DATA_DIR`）。不需要执行能力时设 `AGENTOS_SANDBOX_ENABLED=false`：回退
`StateBackend`（无需服务器，也无 `execute` 工具）。

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
#    → 编辑 .env：设置 OPENAI_BASE_URL、OPENAI_API_KEY、AGENTOS_MODEL

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
│   ├── __init__.py         # 加载 .env
│   ├── model.py            # 网关感知的 chat model 工厂
│   ├── prompts.py          # 系统提示（主 agent + research 子代理）
│   ├── tools.py            # internet_search（Tavily，可选）
│   ├── subagents.py        # research-agent 定义
│   ├── mcp_tools.py        # 按 assistant 载入 .mcp.json 的 MCP 工具
│   ├── runtime.py          # 读取 thread_id / assistant_id
│   ├── sandbox.py          # 每线程沙箱池 + reaper（SessionSandbox / SandboxManager）
│   ├── workspace.py        # 每助手目录 .deepagent/<id>/（skills、.mcp.json、config.json）
│   ├── backends.py         # 组合 CompositeBackend（沙箱 + /assistant/skills/）
│   └── graph.py            # 异步工厂 make_graph(config) 按 assistant 构图  ← 入口
├── .deepagent/<id>/        # 每助手目录（skills/、.mcp.json、config.json；已 gitignore）
└── scripts/
    └── smoke_test.py       # 用 LangGraph SDK 流式跑一轮
```

## 配置

### `aegra.json`

与 `langgraph.json` 同构，这里保持最简：

```json
{
  "dependencies": ["."],
  "graphs": { "agentos": "./agentos/graph.py:make_graph" }
}
```

Aegra 还支持的可选键：`auth`（JWT/OAuth/Firebase/自定义）、`http`（自定义 FastAPI
路由 + CORS）、`store`（带向量嵌入的语义 store）。见
<https://docs.aegra.dev/reference/configuration>。

### 环境变量（`.env`）

| 变量 | 用途 |
| ------------------- | --------------------------------------------------- |
| `OPENAI_BASE_URL`   | 全局网关 base URL（每助手可在 config.json 覆盖；作兜底） |
| `OPENAI_API_KEY`    | 全局网关 key（无鉴权本地网关可填任意值） |
| `AGENTOS_MODEL`     | 全局默认模型名（默认 `gpt-4o`；每助手可在 config.json 覆盖） |
| `AGENTOS_TEMPERATURE` | 可选，采样温度 |
| `TAVILY_API_KEY`    | 可选，为 `research-agent` 开启联网搜索 |
| `AGENTOS_MCP_SERVERS` | 可选 JSON，全局 MCP 工具（叠加每助手 `.mcp.json`） |
| `AGENTOS_SANDBOX_ENABLED` | 是否启用每线程沙箱（默认 `true`；`false` 回退 StateBackend） |
| `OPEN_SANDBOX_DOMAIN` | OpenSandbox 服务器地址（默认 `localhost:8080`） |
| `AGENTOS_SANDBOX_IMAGE` | 沙箱镜像（默认 `python:3.11`） |
| `AGENTOS_SANDBOX_IDLE_TTL` | 沙箱空闲销毁秒数（默认 `1800`） |
| `AGENTOS_DATA_DIR` | 每助手目录根（默认 `.deepagent`） |

## 定制 agent

**加工具** — 在 `agentos/tools.py` 写一个带 docstring 的普通函数，并从 `default_tools()`
返回。它会与 DeepAgents 内置工具（`write_todos`、`ls`、`read_file`、`write_file`、
`edit_file`、`glob`、`grep`、`task` 等）一起暴露。

**加子代理** — 往 `agentos/subagents.py` 的 `build_subagents(model)` 追加一个 dict
（`model` 为工厂传入的该助手模型）：

```python
{
    "name": "my-agent",
    "description": "When the main agent should delegate to me.",
    "system_prompt": "You are ...",
    "tools": [my_tool],   # 可选；覆盖继承的工具
    "model": model,       # 可选；默认沿用该助手模型
}
```

**加 skill**（Agent Skills 规范）— 在 `.deepagent/<assistantId>/skills/<name>/SKILL.md`
放一个技能，`<name>` 须与目录名一致：

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

**接入 MCP 工具**（QuickBooks / ClickUp / 自建）— 在
`.deepagent/<assistantId>/.mcp.json` 的 `mcpServers` 写 server，graph 工厂自动载入为 tools。
每个值遵循 langchain-mcp-adapters 的 Connection schema（`transport` + 对应字段）：

```jsonc
// .deepagent/<assistantId>/.mcp.json
{
  "mcpServers": {
    "fs":      { "transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"] },
    "mspbots": { "transport": "streamable_http", "url": "https://your-host/mcp", "headers": { "Authorization": "Bearer ..." } }
  }
}
```

兼容 Claude/Cursor 风格：可用 `type`（`stdio`/`sse`/`http`）代替 `transport`，或省略（据
`command`/`url` 自动推断）。也可用全局 `AGENTOS_MCP_SERVERS`(JSON) 叠加（助手级同名项覆盖）。

**换模型** — 只是环境变量（`AGENTOS_MODEL`、`OPENAI_BASE_URL`）。若想直接用
Anthropic/Google 而非网关，把 `agentos/model.py` 里的 `ChatOpenAI` 换成
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
