"""MCP tool integration (ready-to-fill stub).

Point OpenAgentOS at any MCP servers — e.g. the MSPbots QuickBooks / ClickUp
servers, or your own — using langchain-mcp-adapters. This is OFF by default so
the project boots with zero external dependencies.

Enable it by setting AGENTOS_MCP_SERVERS to a JSON object of server configs:

    AGENTOS_MCP_SERVERS='{
      "mspbots": {"url": "https://your-host/mcp", "transport": "streamable_http"}
    }'

then wire the loaded tools into the graph. Because tool loading is async, the
cleanest place to add MCP tools is an async graph factory — see the commented
example at the bottom of agentos/graph.py.
"""

from __future__ import annotations

import json
import os


async def load_mcp_tools() -> list:
    """加载 MCP 工具：合并 AGENTOS_MCP_SERVERS（全局）与当前助手 mcp.json（按目录隔离）。

    未配置任何服务器时返回 []。注意：deepagents 在构图时固定工具集，本函数供自定义
    middleware / 异步图工厂在运行时按需调用（见 graph.py 底部说明）。
    """
    from agentos.workspace import assistant_mcp_config

    servers: dict = {}
    raw = os.environ.get("AGENTOS_MCP_SERVERS")
    if raw:
        try:
            servers.update(json.loads(raw))
        except ValueError:
            pass
    # 叠加当前助手 mcp.json（隔离配置），助手级覆盖全局同名项。
    servers.update(assistant_mcp_config())
    if not servers:
        return []

    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(servers)
    return await client.get_tools()
