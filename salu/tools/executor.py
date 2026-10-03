"""The single gate every tool call goes through.

validate input -> evaluate permission -> (ask for approval) -> run with timeout
-> log -> emit events -> return result. Failures are reported, never hidden.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import jsonschema

from ..config import Settings
from ..security.permissions import ApprovalBroker, ApprovalRequest, PermissionPolicy
from ..security.redact import redact, redact_obj
from .base import ToolContext, ToolResult
from .registry import ToolRegistry

log = logging.getLogger("salu.executor")

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
MAX_OUTPUT_CHARS = 20_000


async def _no_events(kind: str, data: dict[str, Any]) -> None:
    return None


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: PermissionPolicy, broker: ApprovalBroker,
                 settings: Settings, events: EventSink | None = None, memory=None):
        self.memory = memory
        self.registry = registry
        self.policy = policy
        self.broker = broker
        self.settings = settings
        self.events: EventSink = events or _no_events
        self.task_id: str | None = None

    async def execute(self, name: str, args: dict[str, Any] | None) -> ToolResult:
        args = dict(args or {})
        tool = self.registry.get(name)
        if tool is None:
            return await self._finish(name, args, ToolResult.failure(f"Unknown tool: {name}"), 0.0, "unknown")

        try:
            jsonschema.validate(args, tool.input_schema)
        except jsonschema.ValidationError as e:
            return await self._finish(name, args, ToolResult.failure(f"Invalid input: {e.message}"), 0.0, "invalid")

        risk = tool.risk_for(args)
        decision = self.policy.evaluate(name, risk)
        safe_args = redact_obj(args)
        log.info("Tool selected: %s risk=%s args=%s", name, decision.risk.label,
                 json.dumps(safe_args, default=str)[:500])
        await self.events("tool_call", {"tool": name, "args": safe_args, "risk": decision.risk.label,
                                        "needs_approval": decision.needs_approval})

        if not decision.allowed:
            return await self._finish(name, args, ToolResult.failure(f"Blocked: {decision.reason}"), 0.0, "blocked")

        if decision.needs_approval:
            req = ApprovalRequest(tool=name, risk=decision.risk,
                                  title=f"{decision.risk.label.upper()} RISK ACTION",
                                  summary=tool.description.split(".")[0],
                                  details=[redact(d) for d in tool.describe(args)])
            await self.events("status", {"state": "awaiting_approval", "detail": f"Waiting for confirmation: {name}"})
            approved = await self.broker.request(req)  # the attached UI displays the dialog
            await self.events("approval_resolved", {"id": req.id, "approved": approved})
            if not approved:
                why = ("Action not approved by the user." if self.broker.attached else
                       "Action not executed: it needs confirmation, but no SALU window is connected to confirm it.")
                return await self._finish(
                    name, args, ToolResult.failure(f"{why} Do not retry it unless the user asks again."), 0.0, "denied")

        ctx = ToolContext(settings=self.settings, logger=logging.getLogger(f"salu.tool.{name}"),
                          task_id=self.task_id, memory=self.memory)
        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(tool.handler(args, ctx), timeout=tool.timeout_seconds)
            if not isinstance(result, ToolResult):
                result = ToolResult.failure(f"Tool returned invalid result type {type(result).__name__}")
        except asyncio.TimeoutError:
            result = ToolResult.failure(f"Tool timed out after {tool.timeout_seconds:.0f}s")
        except Exception as e:
            log.exception("tool crashed: %s", name)
            result = ToolResult.failure(f"{type(e).__name__}: {e}")
        return await self._finish(name, args, result, time.perf_counter() - start, "ran")

    async def _finish(self, name: str, args: dict, result: ToolResult, seconds: float, stage: str) -> ToolResult:
        result.output = redact(result.output or "")
        if len(result.output) > MAX_OUTPUT_CHARS:
            result.output = result.output[:MAX_OUTPUT_CHARS] + f"\n...[truncated {len(result.output) - MAX_OUTPUT_CHARS} chars]"
        level = logging.INFO if result.ok else logging.WARNING
        log.log(level, "Tool %s: %s stage=%s duration=%.2fs%s", "completed" if result.ok else "failed",
                name, stage, seconds, "" if result.ok else f" error={result.output[:300]!r}")
        await self.events("tool_result", {"tool": name, "ok": result.ok, "stage": stage,
                                          "duration": round(seconds, 2), "output": result.output[:2000],
                                          "images": [i["thumb"] for i in (result.images or []) if i.get("thumb")]})
        return result
