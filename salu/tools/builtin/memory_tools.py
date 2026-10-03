"""Memory tools: remember non-secret facts and preferences."""

from __future__ import annotations

import json

from ...memory import SecretRejected
from ..base import ToolResult, salu_tool


@salu_tool("remember",
           "Store a durable, NON-SECRET fact or preference about the user (e.g. project paths, server "
           "names, preferences). Passwords, keys and tokens are refused.",
           risk="low", category="memory",
           properties={"key": {"type": "string", "description": "Short unique label, e.g. 'project.result-system.path'"},
                       "value": {"type": "string"},
                       "category": {"type": "string", "enum": ["preference", "project", "server", "command", "general"]}},
           required=["key", "value"])
async def remember(args, ctx):
    if ctx.memory is None:
        return ToolResult.failure("Memory store is not available.")
    try:
        ctx.memory.remember(args["key"], args["value"], args.get("category", "general"))
    except SecretRejected as e:
        return ToolResult.failure(str(e))
    return ToolResult.success(f"Remembered {args['key']}.")


@salu_tool("recall", "Search stored facts and preferences. Empty query lists recent memories.",
           risk="low", category="memory",
           properties={"query": {"type": "string"}})
async def recall(args, ctx):
    if ctx.memory is None:
        return ToolResult.failure("Memory store is not available.")
    rows = ctx.memory.recall(args.get("query", ""))
    if not rows:
        return ToolResult.success("No matching memories.")
    return ToolResult.success(json.dumps([{k: r[k] for k in ("key", "value", "category")} for r in rows], indent=1))


@salu_tool("forget", "Delete one stored memory by key. Requires confirmation.",
           risk="medium", category="memory",
           properties={"key": {"type": "string"}}, required=["key"])
async def forget(args, ctx):
    if ctx.memory is None:
        return ToolResult.failure("Memory store is not available.")
    if ctx.memory.forget(args["key"]):
        return ToolResult.success(f"Forgot {args['key']}.")
    return ToolResult.failure(f"No memory with key {args['key']!r}.")


TOOLS = [remember, recall, forget]
