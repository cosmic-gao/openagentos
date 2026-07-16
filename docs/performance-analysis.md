# OpenAgentOS 性能分析

> 生成于 2026-07-16。分析对象:`agentos/`(不含 aegra 本体)。**本文只做分析与建议,未改动任何代码。**
> 参考:LangGraph / LangGraph Platform、LangSmith、deepagents 官方文档,以及项目源码逐处核对。

---

## TL;DR(核心结论)

1. **Graph 生命周期 = rebuild 模式(每 run 重建)**。`make_graph(config, runtime)` 签名接受 `config`,按 LangGraph 官方语义属于 "rebuild-at-runtime factory",**每个 run 都会重新执行 `make_graph` 并重新编译整张 graph**。这是官方支持、但明确标注"较慢"的那一档;openagentos 没有用官方任何复用手段。
2. **不存在 config 串号正确性问题**:rebuild 模式每 run 用新 config 重建,配置隔离是对的(此前一度担心的"按 graph_id 缓存导致串号"不成立)。
3. **最大性能杠杆:按 config 指纹缓存编译产物**(openagentos 侧),让相同配置的后续 run 复用已编译 graph,只有首次付编译成本。
4. **构图期的 MCP 加载、skills 扫描是每-run 的真实开销**,且 **LangGraph 节点缓存救不了**(它只缓存节点执行、不缓存构图),只能 openagentos 侧自建缓存(按文件 mtime 失效)。
5. 次级项:沙箱续期阻塞 run 路径、读文件异常绕过失联自愈(即那个 404 硬失败)、LangSmith tracing 采样。

---

## 1. 背景与技术栈

- **框架栈**:Aegra(兼容 LangGraph 的自托管 Agent server)→ `make_graph()` 工厂 → deepagents `create_deep_agent()` → LangChain middleware 栈 + OpenSandbox 后端。
- **模型**:走 OpenAI 兼容网关(`agentos/model.py`);`anthropic:` 前缀时走原生 Anthropic 并激活 prompt caching。
- **每-run 入口**:[`agentos/graph.py:35`](../agentos/graph.py) 的 `make_graph(config, runtime)`。Aegra 通过 `aegra.json` 里 `"agent": "agentos.graph:make_graph"` 找到它(**全项目只注册一个 graph,graph_id 全局唯一**)。

### 每-run 热路径调用链

```
POST /threads/{id}/runs
  └─ (aegra) 加载 graph → make_graph(config, runtime)        agentos/graph.py:35
      ├─ get_settings()                                        @lru_cache ✓ 已复用
      ├─ AgentConfig.model_validate(conf)                      每次解析
      ├─ mcp.tools(_servers(...))                              每次重连所有 MCP server ⚠️
      ├─ sandbox.session(...)                                  轻量句柄 ✓
      ├─ _memory(...) → StoreBackend/CompositeBackend          每次新建
      └─ builder.build(...)                                    每次 create_deep_agent ⚠️
          └─ create_deep_agent(model, tools, middleware, subagents)
              └─ 编译整张 StateGraph(CPU 密集)⚠️⚠️
```

---

## 2. Graph 生命周期定案(整份分析的地基)

### 2.1 LangGraph 官方的两种模式

| 模式 | 行为 | 性能 | 官方出处 |
|---|---|---|---|
| ① 静态图 + 运行时 config 注入 | graph **编译一次复用**,per-request 差异通过 `config["configurable"]` 在**节点运行时**读取 | 高(推荐) | [Assistants](https://docs.langchain.com/langgraph-platform/assistants) |
| ② factory / rebuild-at-runtime | **接受 config 的工厂每 run 被调用**,按 config 重建 graph | 低(官方明标有代价) | [Rebuild graph at runtime](https://docs.langchain.com/langgraph-platform/graph-rebuild) |

官方对模式 ② 的优化处方([reuse compiled graphs](https://langchain-ai.github.io/langgraph/how-tos/compiled-graph-reuse/)):**把编译产物缓存 / 模块级复用,结构差异之外的 per-request 差异走 `config["configurable"]` 运行时注入**,让同一编译实例靠 runtime config 表现不同。

### 2.2 openagentos 属于哪一种

`make_graph(config, runtime)` **签名接受 config** → 归入模式 ②(rebuild)。既然 Aegra 忠实遵循 LangGraph 官方语义,则 **`make_graph` 每个 run 都被调用,重建并编译整张 graph**。openagentos 目前把**所有** config 差异(model / prompt / tools / MCP / assistant_id / review)都放在**构图期**消费,是纯 rebuild,且官方的复用手段一项未用。

### 2.3 如何一次性自测确认(地基验证)

在 `make_graph` 首行加一个进程级计数器,同一 assistant 连发几个 run,看它被调几次:

```python
# agentos/graph.py, make_graph 开头(临时探针,验证后移除)
import itertools, logging
_calls = itertools.count()

async def make_graph(config, runtime):
    logging.getLogger(__name__).warning(
        "make_graph #%d assistant=%s", next(_calls), configurable(config).get("assistant_id")
    )
    ...
```

- 计数**每 run 都涨** → 确认 rebuild 模式 → §5 的缓存优化收益最大。
- 计数**只涨一次**(此后复用)→ Aegra 侧已缓存,重建不是问题,重点转向 §6。

> 结论以 rebuild 为准(签名接受 config + Aegra 遵循官方),但这是地基,建议实测钉死。

---

## 3. 框架三层缓存 / 持久化机制盘点

### 3.1 LangGraph

| 机制 | 官方说法 | openagentos 现状 |
|---|---|---|
| 编译一次、复用 invoke | `CompiledStateGraph` 编译后不可变、是 Runnable,跨调用复用([compile 参考](https://reference.langchain.com/python/langgraph/graph/state/StateGraph/compile)) | ⚠️ rebuild 模式下每 run 重编译 |
| 运行时配置注入(`configurable` / context / `with_config`) | per-request 差异应经 runtime config 注入同一编译 graph | 部分:用了 `configurable` 传参,但用在**构图期** |
| 节点级缓存(`CachePolicy(key_func, ttl)` + `compile(cache=...)`) | **只缓存图执行时节点的输入→输出;不缓存构图阶段**([Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[CachePolicy](https://reference.langchain.com/python/langgraph/types/CachePolicy)) | 未用(且对构图开销无效) |
| 持久化:checkpointer + store | 状态外置的标准层 | ✓ 经 Aegra 用 checkpointer;memory 用 StoreBackend |

**关键辨析**:MCP 加载、skills 扫描都在**构图阶段**(`make_graph` 内),节点缓存**够不到**。

### 3.2 deepagents

- `create_deep_agent` 返回可复用的 `CompiledGraph`,有 `async_create_deep_agent`([参考](https://reference.langchain.com/python/deepagents/graph/create_deep_agent))。
- **Prompt caching**:对 Anthropic/Bedrock 自动缓存 system prompt 静态段(base 指令 + memory + skills),省 token/延迟——openagentos 用 `anthropic:` 前缀时**已自动受益**;走 OpenAI 兼容网关时不生效。
- `cache` 参数:转接 LangGraph 节点缓存;openagentos 未传(如上,对构图开销无用,不传无妨)。
- `SkillsMiddleware`:官方默认把 skills 清单缓存进 thread state(每 thread 首次扫、后续跳过);openagentos 的 `_FreshSkills`([builder.py:21](../agentos/builder.py))**主动绕过**该缓存以换 skills 即时生效。

### 3.3 Aegra

- LangGraph 兼容的自托管 server,提供 Postgres checkpointer/store。
- 忠实遵循 LangGraph 官方语义(据项目维护者说明)→ 对"接受 config 的 factory"按 rebuild 处理。

---

## 4. 性能热点清单(按影响排序)

### 🔴 高

- **每-run 重新 `create_deep_agent` 编译整图** — [builder.py:65](../agentos/builder.py)。重建主 agent + 全 middleware 包装 + research subagent 子图 + 工具绑定,CPU 密集。
- **每-run 重连所有 MCP server + `list_tools`** — [graph.py:47](../agentos/graph.py) → [mcp.py:43](../agentos/mcp.py)。`MultiServerMCPClient(servers).get_tools()` 每 run 重建连接、握手拉工具,多 server 时数百 ms~秒级;且失败静默返回空(**顺带正确性隐患:MCP 抖动会让工具集悄悄消失**)。
- **`_servers()` 在 async 路径同步读文件** — [graph.py:14](../agentos/graph.py)。`read_text()` 阻塞事件循环。

### 🟡 中

- **skills 每-run 重扫磁盘(`_FreshSkills`)** — [builder.py:21](../agentos/builder.py)。刻意绕过 deepagents 的 state 缓存,每 run 一次目录枚举 IO。
- **`_memory` 每-run 新建 CompositeBackend/StoreBackend** — [graph.py:28](../agentos/graph.py)。
- **research subagent 每-run 重建** — [builder.py:70](../agentos/builder.py)。subagent 会被编译成独立子图,叠加编译成本。
- **沙箱续期在 `_acquire` 路径同步 await** — [sandbox.py:208](../agentos/sandbox.py)。命中半-TTL 窗口时给该 run 加一次网络 RTT。
- **读文件走异常路径,绕过失联自愈** — [sandbox.py:241](../agentos/sandbox.py)。`_call` 的自愈只对"失败结果对象"(`_failed`)触发;读文件 404 是**抛异常**,`op(backend)` 直接抛,自愈逻辑没机会跑 → 死沙箱在读路径硬失败(即已观察到的 `proxy/44772/files/download` 404 traceback)。另 `_renew` 失败被吞、不忘记死句柄([sandbox.py:186](../agentos/sandbox.py))。

### 🟢 低 / 待确认

- `AgentConfig.model_validate` 每-run 解析 — [graph.py:39](../agentos/graph.py)。
- `_slots` LRU 锁粒度 — [sandbox.py:157](../agentos/sandbox.py);CPython GIL + 单事件循环下实际安全。
- `secret_redaction` 正则是否模块级预编译 — [redaction.py](../agentos/redaction.py)(**未逐行审计**)。

---

## 5. 优化主线(对齐 LangGraph 官方,按杠杆排序)

### B①〔最大杠杆〕按 config 指纹缓存编译产物 — openagentos 侧

官方 rebuild 模式每 run 调 factory 且**不缓存**;openagentos 不同 assistant 需要不同结构的 graph。在 `make_graph` / `builder.build` 外加**有界 LRU,按 `(assistant_id, config 指纹)` 缓存 `create_deep_agent` 结果**。相同 config 的后续 run 复用编译 graph,只首次付编译成本。编译产物无状态(状态在 checkpointer),复用安全。**不与框架冲突**(框架没做这层),是官方 how-to 思想在多-assistant 场景的推广。

### B②〔向官方靠拢〕把非结构性差异移到 `configurable` 运行时注入 — openagentos 侧重构

model 选择、prompt 文本等**非结构性**差异,改为运行时经 `configurable` 注入(LangChain middleware 支持运行时选 model),而非进构图期。这样它们不进 config 指纹 → B① 的缓存桶变少 → 命中率升。这是把 openagentos 部分迁向官方"静态 + configurable"模式。

### C〔框架管不到〕构图期 IO 单独缓存 — openagentos 侧

- **MCP 工具**:按 `.mcp.json` mtime 缓存清单、复用 client 连接;失败时**保留上次成功清单**而非返回空。
- **skills**:用目录 mtime / TTL 失效替代 `_FreshSkills` 的无条件每-run 重扫(既保即时性,又免绝大多数重复扫描)。

---

## 6. 次级性能项

- **沙箱续期改后台**:`_renew` 用 `asyncio.create_task` fire-and-forget,不阻塞 run 路径([sandbox.py:208](../agentos/sandbox.py))。
- **读路径自愈**:把 `_call` 的失联重建**扩展到异常路径**(try/except → 健康检查 → 忘记死句柄 → 重试一次);`_renew` 失败时也忘记句柄。既提性能韧性,又修 404 硬失败([sandbox.py:241](../agentos/sandbox.py))。
- **LangSmith tracing**:靠 env 开关(未在代码固化,✓)。生产务必**开采样**、避免全量同步上报放大延迟;大 payload / 大 token 会推高成本,按环境降级。

---

## 7. 落地优先级

| 优先 | 动作 | 层 | 收益 |
|---|---|---|---|
| 1 | B① 按 config 指纹缓存编译 graph | openagentos | 消除相同 config 的重复编译(最大) |
| 2 | C MCP/skills 构图 IO 缓存(mtime 失效) | openagentos | 削首次/低命中时的秒级 IO |
| 3 | sandbox 后台续期 + 异常自愈 | openagentos | 去 run 路径 RTT、修 404 硬失败 |
| 4 | B② 差异移 configurable | openagentos | 提升 #1 命中率(渐进重构) |
| 5 | tracing 采样 | 配置 | 降 tracing 延迟/成本 |

> 动手前先做 §2.3 的自测钉死 graph 生命周期,再从 #1 开始。

---

## 8. 覆盖边界与待办

**已逐行审计**:`config.py`、`builder.py`、`graph.py`、`middleware.py`、`model.py`、`sandbox.py`、`mcp.py`。

**尚未逐行审计**(仅从调用点间接涉及,需补查以确保不遗漏次级项):`workspace.py`、`tools.py`、`sweeper.py`、`redaction.py`、`routes.py`、`assets.py`。重点关注:`workspace.py` 的路径构造 / `mkdir` / IO(每 run 被 graph.py 调);`tools.py` 的 `build_download` 每 run 构造;`sweeper.py`(疑似沙箱/工作区清理后台任务);`redaction.py` 正则是否预编译;`routes.py` / `assets.py` 的文件下载是否有同步 IO / 无分页。

---

## 附:官方参考链接

- LangGraph — Rebuild graph at runtime: https://docs.langchain.com/langgraph-platform/graph-rebuild
- LangGraph — Reuse compiled graphs across runs: https://langchain-ai.github.io/langgraph/how-tos/compiled-graph-reuse/
- LangGraph — Assistants: https://docs.langchain.com/langgraph-platform/assistants
- LangGraph — Graph API(节点缓存): https://docs.langchain.com/oss/python/langgraph/graph-api
- LangGraph — CachePolicy 参考: https://reference.langchain.com/python/langgraph/types/CachePolicy
- LangGraph — configuration(configurable fields): https://langchain-ai.github.io/langgraph/how-tos/configuration/
- deepagents — create_deep_agent 参考: https://reference.langchain.com/python/deepagents/graph/create_deep_agent
- deepagents — overview: https://docs.langchain.com/oss/python/deepagents/overview
