"""OpenAgentOS — a self-hosted AI agent OS.

DeepAgents provides the agent harness (planning, virtual filesystem, subagents,
skills); Aegra hosts the resulting LangGraph graph with PostgreSQL persistence,
streaming and the Agent Protocol API.

Importing this package loads a local `.env` (if present) so configuration is
available whether the graph is loaded by Aegra, a script, or a test.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

__version__ = "0.1.0"
__all__ = ["__version__"]
