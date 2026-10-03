"""Action-permission system.

Every tool execution passes through PermissionPolicy.evaluate() and, when
required, an ApprovalBroker. This lives in the core — not in the AI provider —
so swapping the brain can never bypass it.

Rules (enforced in code, not configurable away):
  * HIGH risk always requires explicit confirmation.
  * Risk overrides can only RAISE a tool's risk, never lower it.
  * No approver connected, timeout, or error => DENY.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("salu.security")


class Risk(enum.IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @classmethod
    def parse(cls, value: "str | Risk") -> "Risk":
        if isinstance(value, Risk):
            return value
        return cls[str(value).strip().upper()]

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    needs_approval: bool
    risk: Risk
    reason: str


@dataclass
class ApprovalRequest:
    tool: str
    risk: Risk
    title: str
    summary: str
    details: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict:
        return {"id": self.id, "tool": self.tool, "risk": self.risk.label, "title": self.title,
                "summary": self.summary, "details": self.details}


class PermissionPolicy:
    def __init__(self, auto_approve: list[str] | None = None,
                 risk_overrides: dict[str, str] | None = None,
                 disabled_tools: list[str] | None = None):
        auto = {Risk.parse(r) for r in (auto_approve or ["low"])}
        auto.discard(Risk.HIGH)  # HIGH can never be auto-approved
        self.auto_approve = frozenset(auto)
        self.risk_overrides = {k: Risk.parse(v) for k, v in (risk_overrides or {}).items()}
        self.disabled_tools = frozenset(disabled_tools or [])

    def effective_risk(self, tool_name: str, base: Risk) -> Risk:
        override = self.risk_overrides.get(tool_name)
        return max(base, override) if override else base

    def evaluate(self, tool_name: str, base_risk: Risk) -> PermissionDecision:
        if tool_name in self.disabled_tools:
            return PermissionDecision(False, False, base_risk, "tool disabled in settings")
        risk = self.effective_risk(tool_name, base_risk)
        if risk == Risk.HIGH:
            return PermissionDecision(True, True, risk, "high risk: explicit confirmation required")
        if risk in self.auto_approve:
            return PermissionDecision(True, False, risk, f"{risk.label} risk: auto-approved")
        return PermissionDecision(True, True, risk, f"{risk.label} risk: confirmation required")


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[None]]


class ApprovalBroker:
    """Routes approval requests to whichever UI is attached and waits for an answer.

    A UI calls `attach(handler)`; the handler must display the request and later
    call `resolve(request_id, approved)`. With no handler attached, requests are
    denied immediately.
    """

    def __init__(self, timeout_seconds: float = 120):
        self.timeout = timeout_seconds
        self._handler: ApprovalHandler | None = None
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._requests: dict[str, ApprovalRequest] = {}

    def attach(self, handler: ApprovalHandler | None) -> None:
        self._handler = handler

    @property
    def attached(self) -> bool:
        return self._handler is not None

    def pending(self) -> list[ApprovalRequest]:
        return [self._requests[k] for k in self._pending if k in self._requests]

    async def request(self, req: ApprovalRequest) -> bool:
        if self._handler is None:
            log.warning("approval denied: no approver connected tool=%s risk=%s", req.tool, req.risk.label)
            return False
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[req.id] = fut
        self._requests[req.id] = req
        log.info("approval requested id=%s tool=%s risk=%s", req.id, req.tool, req.risk.label)
        try:
            await self._handler(req)
            approved = await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            log.warning("approval timed out id=%s tool=%s -> denied", req.id, req.tool)
            approved = False
        except Exception:
            log.exception("approval failed id=%s tool=%s -> denied", req.id, req.tool)
            approved = False
        finally:
            self._pending.pop(req.id, None)
            self._requests.pop(req.id, None)
        log.info("approval %s id=%s tool=%s", "granted" if approved else "denied", req.id, req.tool)
        return approved

    def resolve(self, request_id: str, approved: bool) -> bool:
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(bool(approved))
        return True

    def deny_all(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result(False)
