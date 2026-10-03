"""Agent runtime: turns a user request into a tracked task.

request -> task record -> brain (plans + calls tools through the executor)
-> events streamed to the UI -> task finished (done / failed) with summary.
Failures are always surfaced as an `error` event and a failed task.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..brain.base import Brain, BrainSession
from ..config import Settings
from ..memory import MemoryStore
from ..security.permissions import ApprovalBroker, PermissionPolicy
from ..tools.executor import ToolExecutor
from ..tools.registry import ToolRegistry
from .prompt import build_system_prompt

log = logging.getLogger("salu.agent")

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class AgentSession:
    id: str
    executor: ToolExecutor
    brain_session: BrainSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sink: EventSink | None = None


class Agent:
    def __init__(self, settings: Settings, brain: Brain, registry: ToolRegistry, policy: PermissionPolicy,
                 broker: ApprovalBroker, memory: MemoryStore):
        self.settings = settings
        self.brain = brain
        self.registry = registry
        self.policy = policy
        self.broker = broker
        self.memory = memory
        self.sessions: dict[str, AgentSession] = {}
        self.state = "idle"
        self.current_task: dict[str, Any] | None = None

    def _session(self, session_id: str) -> AgentSession:
        s = self.sessions.get(session_id)
        if s is None:
            holder: dict[str, AgentSession] = {}

            async def forward(kind: str, data: dict) -> None:
                sess = holder.get("s")
                if sess and sess.sink:
                    await sess.sink(kind, data)

            executor = ToolExecutor(self.registry, self.policy, self.broker, self.settings,
                                    events=forward, memory=self.memory)
            tool_names = [t.name for t in self.registry.all() if t.name not in self.policy.disabled_tools]
            prompt = build_system_prompt(self.settings.assistant_name, self.settings.user_name,
                                         self.memory.recall(limit=40), tool_names)
            s = AgentSession(session_id, executor, self.brain.new_session(executor, self.registry, prompt))
            holder["s"] = s
            self.sessions[session_id] = s
        return s

    async def handle(self, text: str, sink: EventSink, session_id: str = "default") -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reply": ""}
        session = self._session(session_id)
        async with session.lock:
            task_id = self.memory.start_task(text, session_id)
            self.memory.add_message(session_id, "user", text)
            tools_used: list[str] = []
            started = time.time()

            async def emit(kind: str, data: dict) -> None:
                if kind == "tool_call":
                    tools_used.append(data["tool"])
                    await self._set_state(sink, "executing", f"Running {data['tool']}")
                elif kind == "tool_result":
                    await self._set_state(sink, "thinking", "Analysing results")
                elif kind == "status":
                    self.state = data.get("state", self.state)
                try:
                    await sink(kind, {**data, "task_id": task_id})
                except Exception:
                    log.debug("event sink failed", exc_info=True)

            session.sink = emit
            session.executor.task_id = task_id
            self.current_task = {"id": task_id, "request": text, "started_at": started}
            log.info("User request received task=%s chars=%d", task_id, len(text))
            await emit("task_started", {"request": text})
            await self._set_state(emit, "thinking", "Planning")

            reply_parts: list[str] = []
            error: str | None = None
            try:
                async for ev in session.brain_session.send(text):
                    if ev.kind == "text":
                        reply_parts.append(ev.text)
                        await emit("text", {"text": ev.text})
                    elif ev.kind == "error":
                        error = ev.text
                    elif ev.kind == "done":
                        await emit("usage", ev.data)
            except Exception as e:
                log.exception("agent failure task=%s", task_id)
                error = f"Internal error: {type(e).__name__}: {e}"

            reply = "\n\n".join(reply_parts).strip()
            status = "failed" if error else "done"
            summary = error or (reply.splitlines()[0][:200] if reply else "Completed")
            self.memory.finish_task(task_id, status, summary, tools_used)
            if reply:
                self.memory.add_message(session_id, "assistant", reply)
            if error:
                log.error("Task failed task=%s error=%s", task_id, error)
                await emit("error", {"message": error})
            else:
                log.info("Response generated task=%s tools=%s seconds=%.1f", task_id, tools_used, time.time() - started)
            await emit("task_finished", {"status": status, "summary": summary, "tools_used": tools_used,
                                         "seconds": round(time.time() - started, 1)})
            self.current_task = None
            session.sink = None
            await self._set_state(sink, "idle", "")
            return {"ok": error is None, "reply": reply, "error": error, "task_id": task_id, "tools_used": tools_used}

    async def _set_state(self, sink: EventSink, state: str, detail: str) -> None:
        self.state = state
        try:
            await sink("status", {"state": state, "detail": detail})
        except Exception:
            log.debug("status sink failed", exc_info=True)

    async def interrupt(self, session_id: str = "default") -> None:
        s = self.sessions.get(session_id)
        self.broker.deny_all()
        if s:
            await s.brain_session.interrupt()

    async def reset_session(self, session_id: str = "default") -> None:
        s = self.sessions.pop(session_id, None)
        if s:
            await s.brain_session.close()

    async def shutdown(self) -> None:
        self.broker.deny_all()
        for sid in list(self.sessions):
            await self.reset_session(sid)


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]
