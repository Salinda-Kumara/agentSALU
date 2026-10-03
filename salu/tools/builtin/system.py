"""System information tools (read-only, LOW risk)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from ...system_info import full_status
from ..base import ToolResult, salu_tool


@salu_tool("system_status",
           "Check this Windows computer's health: CPU, RAM, disks, uptime, top memory processes, network. "
           "Read-only.",
           risk="low", category="system")
async def system_status(args, ctx):
    data = await asyncio.to_thread(full_status)
    return ToolResult.success(json.dumps(data, indent=1), data)


@salu_tool("current_datetime", "Get the current local date, time, weekday and timezone.",
           risk="low", category="system")
async def current_datetime(args, ctx):
    now = datetime.now().astimezone()
    return ToolResult.success(f"{now:%A, %Y-%m-%d %H:%M:%S} (UTC{now:%z}, {now.tzname()})")


TOOLS = [system_status, current_datetime]
